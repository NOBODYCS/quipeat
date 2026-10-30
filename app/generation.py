import concurrent.futures
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path

from pydantic import BaseModel, Field

from . import db
from .config import DEFAULT_LEVEL, LEVELS, settings
from .llm.mock import MockProvider
from .llm.providers import make_provider
from .llm.structured import structured_call
from .security import decrypt_secret
from .i18n import translate
from .ingest import extract_text_from_url, chunk_text

PROMPT_VERSION = "v1"
MIN_VERIFY_SCORE = 0.7

log = logging.getLogger("quipeat.generation")


class _JobEventStore(logging.Handler):
    """Buffers job log lines and flushes them to job_events in one batch per
    chunk — a thorough deck logs ~1000 lines, and per-line INSERT+commit
    under the global DB lock would serialize against live traffic."""

    def __init__(self, job_id: str):
        super().__init__()
        self.job_id = job_id
        self.buffer: list[tuple[str, str]] = []
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        try:
            self.buffer.append((record.levelname.lower(), self.format(record)[:1000]))
        except Exception:
            pass

    def flush_batch(self):
        if not self.buffer:
            return
        rows, self.buffer = self.buffer, []
        try:
            with db._lock:
                conn = db.get_conn()
                conn.executemany(
                    "INSERT INTO job_events(job_id, level, message) VALUES (?, ?, ?)",
                    [(self.job_id, lvl, msg) for lvl, msg in rows])
                conn.commit()
        except Exception:
            pass


JOB_EVENT_RETENTION_DAYS = 14


def prune_job_events() -> None:
    """Keep storage bounded: drop events past retention and events whose job
    (or its deck) no longer exists. Called after each job and at startup."""
    try:
        db.execute("DELETE FROM job_events WHERE created_at < datetime('now', ?)",
                   (f'-{JOB_EVENT_RETENTION_DAYS} days',))
        db.execute("DELETE FROM job_events WHERE job_id NOT IN (SELECT id FROM jobs)")
    except Exception:
        pass


def _attach_job_logger(job_id: str) -> logging.Handler:
    handler = _JobEventStore(job_id)
    handler.setLevel(logging.INFO)
    if log.getEffectiveLevel() > logging.INFO:
        log.setLevel(logging.INFO)
    log.addHandler(handler)
    return handler

_PROMPTS = {
    "generate": Path(__file__).parent / "prompts" / f"generate_{PROMPT_VERSION}.md",
    "verify": Path(__file__).parent / "prompts" / f"verify_{PROMPT_VERSION}.md",
    "triage": Path(__file__).parent / "prompts" / f"triage_{PROMPT_VERSION}.md",
}


class QuestionDraft(BaseModel):
    prompt: str
    options: list[str] = Field(min_length=4, max_length=4)
    correct_index: int = Field(ge=0, le=3)
    explanation: str = ""
    difficulty: str = "medium"


class GenerateResponse(BaseModel):
    questions: list[QuestionDraft]


class ChunkVerdict(BaseModel):
    index: int = Field(ge=0)
    is_learning_material: bool = True
    reason: str = ""


class TriageResponse(BaseModel):
    verdicts: list[ChunkVerdict]


class VerifyItem(BaseModel):
    index: int = Field(ge=0)
    verdict: str
    score: float = Field(ge=0.0, le=1.0)
    notes: str = ""


class VerifyResponse(BaseModel):
    results: list[VerifyItem]


def _system_prompt(name: str) -> str:
    return _PROMPTS[name].read_text()


def _get_provider(user_id: int):
    if settings.llm_mock:
        return MockProvider(), "mock-model"
    us = db.query_one("SELECT * FROM user_settings WHERE user_id = ?", (user_id,))
    user = db.query_one("SELECT role FROM users WHERE id = ?", (user_id,))
    if us and us["default_provider"] == "builtin":
        if user["role"] not in ("admin", "privileged"):
            raise RuntimeError("builtin_denied")
        if not settings.mistral_api_key:
            raise RuntimeError("builtin_no_server_key")
        return make_provider("mistral", settings.mistral_api_key), settings.builtin_model
    if not us or not us["active_key_id"]:
        raise RuntimeError("no_llm_key")
    key = db.query_one("SELECT * FROM api_keys WHERE id = ? AND user_id = ?",
                       (us["active_key_id"], user_id))
    if not key:
        raise RuntimeError("no_key_builtin")
    provider = make_provider(key["provider"], decrypt_secret(key["encrypted_key"]))
    return provider, us["default_model"]


def _normalize_options(q: QuestionDraft) -> QuestionDraft:
    seen, options = set(), []
    for opt in q.options:
        opt = opt.strip()
        key = opt.lower()
        if key and key not in seen:
            seen.add(key)
            options.append(opt)
    while len(options) < 4:
        options.append(f"Distractor {len(options) + 1}")
    q.options = options[:4]
    if q.correct_index >= len(q.options):
        q.correct_index = 0
    return q


def _similarity(a: str, b: str) -> float:
    ta = set(re.findall(r"[a-z0-9]+", a.lower()))
    tb = set(re.findall(r"[a-z0-9]+", b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


LANG_NAMES = {"en": "English", "pl": "Polish (polski)"}

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "guide", "how", "in", "into", "intro", "is", "it", "of", "on", "or",
    "quiz", "that", "the", "this", "to", "was", "what", "when", "with",
    "basics", "fundamentals", "about", "my", "your",
}


def _words(text: str) -> list[str]:
    """Unicode-aware word tokens, so Polish/other diacritics survive."""
    return re.findall(r"[^\W_]+", text.lower(), re.UNICODE)


def _topic_terms(title: str) -> set[str]:
    return {t for t in _words(title) if len(t) > 2 and t not in _STOPWORDS}


def _topic_score(chunk_text: str, terms: set[str]) -> float:
    """Cheap keyword-overlap triage: how much of this chunk is about the topic."""
    if not terms:
        return 0.0
    words = _words(chunk_text)
    if not words:
        return 0.0
    hits = sum(1 for w in words if w in terms)
    return hits / len(words)


def _semantic_triage(provider, model, deck_title: str, chunks, job_id: str) -> list:
    """One cheap LLM call per batch of chunks: classify each excerpt as
    learning material for the topic or not. Works for any source (URL, PDF,
    pasted text) because it judges CONTENT, not URL patterns. Chunks judged
    not-learning-material are dropped before any generation budget is spent
    on them."""
    kept = []
    batch = 8
    for start in range(0, len(chunks), batch):
        group = chunks[start:start + batch]
        excerpts = "\n".join(
            f"--- Excerpt {i} ---\n{c['text'][:1200]}" for i, c in enumerate(group))
        user_msg = (
            f"TOPIC: a learner wants to master: {deck_title}.\n\n"
            f"Classify each excerpt: is it learning material about the TOPIC that a "
            f"practitioner/student would study (concepts, usage, commands, configuration, "
            f"behavior, examples)? Or is it about something else (project contribution/ "
            f"governance, changelogs, licensing, site navigation, unrelated content)?\n\n"
            f"{excerpts}"
        )
        try:
            resp, _ = structured_call(provider, model, _system_prompt("triage"),
                                      user_msg, TriageResponse, temperature=0.0,
                                      max_tokens=512)
            keep = {v.index for v in resp.verdicts if v.is_learning_material}
        except Exception as e:
            log.warning("[job %s] triage call failed (%s: %s), keeping all chunks",
                        job_id, type(e).__name__, str(e)[:150])
            keep = set(range(len(group)))
        for i, c in enumerate(group):
            if i in keep:
                kept.append(c)
            else:
                log.info("[job %s] triage dropped chunk %s (not learning material)",
                         job_id, c["idx"])
    return kept


def _lang_instruction(lang: str) -> str:
    name = LANG_NAMES.get(lang, "English")
    if lang == "pl":
        return ("\n\nLANGUAGE: Write ALL questions, options, explanations and difficulty labels "
                "in POLISH (polski). Use natural, correct Polish. The source text may be in any "
                "language — the quiz output must still be in Polish.")
    return ("\n\nLANGUAGE: Write ALL questions, options, explanations and difficulty labels "
            "in ENGLISH. The source text may be in any language — the quiz output must still be in English.")


def generate_deck_questions(user_id: int, deck_id: int, job_id: str) -> None:
    """Runs as a background thread: for every chunk, draft questions, verify each
    against its chunk, dedup, and insert passing ones. Progress is tracked in jobs."""
    job_handler = _attach_job_logger(job_id)
    try:
        _generate_deck_questions_inner(user_id, deck_id, job_id, job_handler)
    finally:
        job_handler.flush_batch()
        log.removeHandler(job_handler)
        prune_job_events()


def _generate_deck_questions_inner(user_id: int, deck_id: int, job_id: str,
                                   job_handler) -> None:
    job = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    deck = db.query_one(
        "SELECT d.*, s.kind AS source_kind, s.original_ref FROM decks d"
        " JOIN sources s ON s.id = d.source_id WHERE d.id = ?", (deck_id,))
    gen_lang = deck["lang"] if "lang" in deck.keys() and deck["lang"] in LANG_NAMES else "en"
    level = deck["level"] if "level" in deck.keys() and deck["level"] in LEVELS else DEFAULT_LEVEL
    goal = deck["goal"] if "goal" in deck.keys() and deck["goal"].strip() else deck["title"]
    log.info("[job %s] deck=%s title=%r goal=%r kind=%s level=%s lang=%s",
             job_id, deck_id, deck["title"], goal[:120], deck["source_kind"], level, gen_lang)
    cfg = LEVELS[level]
    questions_per_chunk = cfg["questions_per_chunk"]
    target_questions = cfg["target_questions"]

    # URL sources: crawl now (in the background), store text + chunks.
    # The POST returned immediately; the job bar polls for progress.
    if deck["source_kind"] == "url":
        db.execute("UPDATE jobs SET status='running', total=0, started_at=datetime('now'),"
                   " message='Fetching and reading the link…' WHERE id=?", (job_id,))
        log.info("[job %s] crawling url=%s max_pages=%s",
                 job_id, deck["original_ref"], cfg["max_pages"])
        try:
            raw = extract_text_from_url(deck["original_ref"], max_pages=cfg["max_pages"])
        except Exception as e:
            log.error("[job %s] crawl failed: %s: %s", job_id, type(e).__name__, e)
            ulang = (db.query_one("SELECT lang FROM users WHERE id = ?", (user_id,)) or {"lang": "en"})["lang"]
            key = str(e)
            msg = translate(ulang, key[2:]) if key.startswith("__") else str(e)[:200]
            db.execute("UPDATE jobs SET status='failed', error=?, finished_at=datetime('now') WHERE id=?",
                       (msg, job_id))
            db.execute("UPDATE decks SET status='failed' WHERE id=?", (deck_id,))
            return
        if not raw.strip():
            db.execute("UPDATE jobs SET status='failed', error='No readable text found at this link.',"
                       " finished_at=datetime('now') WHERE id=?", (job_id,))
            db.execute("UPDATE decks SET status='failed' WHERE id=?", (deck_id,))
            return
        log.info("[job %s] crawl done: %s words extracted", job_id, len(raw.split()))
        db.execute("UPDATE sources SET raw_text=? WHERE id=?", (raw, deck["source_id"]))
        pieces = chunk_text(raw)
        for idx, piece in enumerate(pieces):
            db.execute("INSERT INTO chunks(source_id, idx, text) VALUES (?, ?, ?)",
                       (deck["source_id"], idx, piece))
        log.info("[job %s] chunked into %s pieces", job_id, len(pieces))

    chunks = db.query(
        "SELECT * FROM chunks WHERE source_id = ? ORDER BY idx", (deck["source_id"],))

    # Triage FIRST, then sample. Cheap keyword scoring picks a candidate pool
    # (top 2x budget) from all chunks; the LLM then classifies that pool as
    # learning material or not; the question budget is sampled from the
    # SURVIVORS — so garbage chunks never consume budget and decks fill
    # their target even when the crawl grabbed nav/legal/contributor pages.
    needed = max(1, -(-target_questions // questions_per_chunk))
    log.info("[job %s] %s chunks available, budget=%s chunks (target=%s q, %s q/chunk)",
             job_id, len(chunks), needed, target_questions, questions_per_chunk)
    pool = chunks
    if len(chunks) > needed:
        terms = _topic_terms(goal)
        scored = [(_topic_score(c["text"], terms), i, c) for i, c in enumerate(chunks)]
        top = min(len(chunks), max(needed, needed * 2))
        log.info("[job %s] triage: topic terms=%s, scores min=%.4f max=%.4f mean=%.4f",
                 job_id, sorted(terms)[:10],
                 min(t[0] for t in scored), max(t[0] for t in scored),
                 sum(t[0] for t in scored) / len(scored))
        best = sorted(scored, key=lambda t: (-t[0], t[1]))[:top]
        pool = [t[2] for t in sorted(best, key=lambda t: t[1])]
        log.info("[job %s] triage: keyword pool = top-%s of %s", job_id, len(pool), len(chunks))

    try:
        provider, model = _get_provider(user_id)
        log.info("[job %s] provider=%s model=%s", job_id, provider.name, model)
        db.execute("UPDATE jobs SET status='running', total=?, started_at=datetime('now') WHERE id=?",
                   (len(chunks), job_id))
        if len(pool) > 1 or pool != chunks:
            pool = _semantic_triage(provider, model, goal, pool, job_id)
        log.info("[job %s] semantic triage kept %s/%s pool chunks",
                 job_id, len(pool), len(chunks))
        db.execute("UPDATE jobs SET total=? WHERE id=?", (len(pool), job_id))

        chunks = pool
        if len(chunks) > needed:
            step = len(chunks) / needed
            picked = [chunks[min(len(chunks) - 1, int(i * step))] for i in range(needed)]
            seen_idx = set()
            deduped = []
            for c in picked:
                if c["id"] not in seen_idx:
                    seen_idx.add(c["id"])
                    deduped.append(c)
            chunks = deduped
            log.info("[job %s] sampled %s chunks from %s survivors",
                     job_id, len(chunks), len(pool))
        seen_prompts: list[str] = []
        for chunk in chunks:
            log.info("[job %s] chunk %s: drafting (%s words)", job_id, chunk["idx"],
                     len(chunk["text"].split()))
            user_msg = (
                f"LEARNING GOAL: The user wants to learn: {goal}.\n"
                f"This text is one excerpt from their chosen source of truth. Generate"
                f" questions that serve this learning goal.\n\n"
                f"Source text:\n\"\"\"\n{chunk['text']}\n\"\"\"\n\n"
                f"Generate {questions_per_chunk} multiple-choice questions from the source text above."
                + cfg["difficulty_hint"] + _lang_instruction(gen_lang)
            )
            try:
                drafts, gen_result = structured_call(provider, model, _system_prompt("generate"),
                                                     user_msg, GenerateResponse, temperature=0.2,
                                                     max_tokens=2048)
                log.info("[job %s] chunk %s: %s drafts (in=%s out=%s tokens)",
                         job_id, chunk["idx"], len(drafts.questions),
                         gen_result.input_tokens, gen_result.output_tokens)
            except Exception as e:
                log.error("[job %s] chunk %s: generation failed: %s: %s",
                          job_id, chunk["idx"], type(e).__name__, str(e)[:300])
                db.execute("UPDATE jobs SET done=done+1, message=? WHERE id=?",
                           (f"chunk {chunk['idx']}: generation failed: {str(e)[:200]}", job_id))
                continue
            candidates = []
            for draft in drafts.questions:
                q = _normalize_options(draft)
                if any(_similarity(q.prompt, s) > 0.85 for s in seen_prompts):
                    log.info("[job %s] chunk %s: skipped duplicate prompt %r",
                             job_id, chunk["idx"], q.prompt[:80])
                    continue
                candidates.append(q)

            scores: dict[int, float] = {}
            if candidates:
                questions_block = "\n".join(
                    f"Question {i}: {q.prompt}\nOptions: {json.dumps(q.options)}\n"
                    f"Claimed correct index: {q.correct_index}\n"
                    f"Explanation: {q.explanation}"
                    for i, q in enumerate(candidates))
                verify_msg = (
                    f"Source text:\n\"\"\"\n{chunk['text']}\n\"\"\"\n\n"
                    f"{questions_block}"
                )
                try:
                    verdict, _ = structured_call(provider, model, _system_prompt("verify"),
                                                 verify_msg, VerifyResponse, temperature=0.0,
                                                 max_tokens=1024)
                    for item in verdict.results:
                        if 0 <= item.index < len(candidates):
                            scores[item.index] = (
                                item.score if item.verdict == "pass" else 0.0)
                    log.info("[job %s] chunk %s: batch verify: %s/%s passed",
                             job_id, chunk["idx"], len(scores), len(candidates))
                except Exception as e:
                    log.warning("[job %s] chunk %s: batch verify failed (%s: %s), assuming 0.5",
                                job_id, chunk["idx"], type(e).__name__, str(e)[:150])
                    scores = {i: 0.5 for i in range(len(candidates))}

            reached_target = False
            if not db.query_one("SELECT 1 FROM decks WHERE id = ?", (deck_id,)):
                log.info("[job %s] deck %s deleted mid-run; stopping", job_id, deck_id)
                db.execute("UPDATE jobs SET status='done', finished_at=datetime('now'),"
                           " message='Deck deleted during generation.' WHERE id=?", (job_id,))
                return
            for i, q in enumerate(candidates):
                score = scores.get(i, 0.5)
                if score < MIN_VERIFY_SCORE:
                    log.info("[job %s] chunk %s: rejected by verify (score=%s < %s): %r",
                             job_id, chunk["idx"], score, MIN_VERIFY_SCORE, q.prompt[:80])
                    continue
                inserted = db.query_one(
                    "SELECT COUNT(*) AS n FROM questions WHERE deck_id = ?", (deck_id,))["n"]
                if inserted >= target_questions:
                    db.execute("UPDATE jobs SET done=done+1, message=? WHERE id=?",
                               (f"Target reached ({target_questions} questions).", job_id))
                    reached_target = True
                    break
                db.execute(
                    "INSERT INTO questions(deck_id, chunk_id, prompt, options_json, correct_index,"
                    " explanation, difficulty, model, prompt_version, verify_score)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (deck_id, chunk["id"], q.prompt, json.dumps(q.options), q.correct_index,
                     q.explanation, q.difficulty, model, PROMPT_VERSION, score),
                )
                seen_prompts.append(q.prompt)
            if reached_target:
                break
            db.execute("UPDATE jobs SET done=done+1 WHERE id=?", (job_id,))
            job_handler.flush_batch()
        inserted = db.query_one("SELECT COUNT(*) AS n FROM questions WHERE deck_id = ?", (deck_id,))["n"]
        log.info("[job %s] finished: %s questions inserted (target was %s)",
                 job_id, inserted, target_questions)
        msg = "Generation complete" if inserted else (
            "Generation complete, but the source contained little substantive content"
            " about the topic — try a more specific link or paste the text directly.")
        db.execute("UPDATE jobs SET status='done', finished_at=datetime('now'), message=? WHERE id=?",
                   (msg, job_id))
        db.execute("UPDATE decks SET status='ready' WHERE id=?", (deck_id,))
    except Exception as e:
        log.exception("[job %s] generation crashed: %s", job_id, e)
        ulang = (db.query_one("SELECT lang FROM users WHERE id = ?", (user_id,)) or {"lang": "en"})["lang"]
        msg = str(e)
        if msg in ("builtin_denied", "builtin_no_server_key", "no_llm_key", "no_key_builtin"):
            msg = translate(ulang, msg)
        db.execute("UPDATE jobs SET status='failed', error=?, finished_at=datetime('now') WHERE id=?",
                   (msg[:500], job_id))
        db.execute("UPDATE decks SET status='failed' WHERE id=?", (deck_id,))


_EXECUTOR = None
_EXECUTOR_LOCK = threading.Lock()
MAX_CONCURRENT_JOBS = 3


def _get_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Bounded worker pool: deck builds cap at MAX_CONCURRENT_JOBS instead of
    spawning an unbounded thread per job (10 users x thorough decks would
    otherwise run 10 browser+LLM pipelines in parallel)."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                max_workers=MAX_CONCURRENT_JOBS, thread_name_prefix="deckgen")
        return _EXECUTOR


def recover_stuck_jobs() -> None:
    """Startup recovery: jobs interrupted by a container restart are stuck
    'running'/'queued' forever. Mark them failed and reset their decks so
    regen works."""
    stuck = db.query("SELECT id, deck_id FROM jobs WHERE status IN ('queued', 'running')")
    for job in stuck:
        db.execute(
            "UPDATE jobs SET status='failed',"
            " error='Interrupted by server restart — regenerate this deck.',"
            " finished_at=datetime('now') WHERE id = ?", (job["id"],))
        db.execute("UPDATE decks SET status='draft' WHERE id = ?", (job["deck_id"],))
    if stuck:
        log.warning("startup recovery: marked %s interrupted job(s) failed", len(stuck))


def start_generation(user_id: int, deck_id: int) -> str:
    job_id = uuid.uuid4().hex
    db.execute("INSERT INTO jobs(id, user_id, deck_id) VALUES (?, ?, ?)", (job_id, user_id, deck_id))
    _get_executor().submit(generate_deck_questions, user_id, deck_id, job_id)
    return job_id
