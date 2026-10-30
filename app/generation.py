import json
import logging
import re
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

_PROMPTS = {
    "generate": Path(__file__).parent / "prompts" / f"generate_{PROMPT_VERSION}.md",
    "verify": Path(__file__).parent / "prompts" / f"verify_{PROMPT_VERSION}.md",
}


class QuestionDraft(BaseModel):
    prompt: str
    options: list[str] = Field(min_length=4, max_length=4)
    correct_index: int = Field(ge=0, le=3)
    explanation: str = ""
    difficulty: str = "medium"


class GenerateResponse(BaseModel):
    questions: list[QuestionDraft]


class VerifyResponse(BaseModel):
    verdict: str
    score: float = Field(ge=0.0, le=1.0)
    notes: str = ""


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
    job = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    deck = db.query_one(
        "SELECT d.*, s.kind AS source_kind, s.original_ref FROM decks d"
        " JOIN sources s ON s.id = d.source_id WHERE d.id = ?", (deck_id,))
    gen_lang = deck["lang"] if "lang" in deck.keys() and deck["lang"] in LANG_NAMES else "en"
    level = deck["level"] if "level" in deck.keys() and deck["level"] in LEVELS else DEFAULT_LEVEL
    log.info("[job %s] deck=%s title=%r kind=%s level=%s lang=%s",
             job_id, deck_id, deck["title"], deck["source_kind"], level, gen_lang)
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

    # Triage, then sample. Score every chunk against the topic (keyword
    # overlap with the deck title) and prefer the most on-topic chunks, so a
    # big crawl spends its question budget on substantive pages instead of
    # evenly across nav/legal/changelog filler. Ties and near-zero scores
    # keep the original order (even spread across the source).
    needed = max(1, -(-target_questions // questions_per_chunk))
    log.info("[job %s] %s chunks available, budget=%s chunks (target=%s q, %s q/chunk)",
             job_id, len(chunks), needed, target_questions, questions_per_chunk)
    if len(chunks) > needed:
        terms = _topic_terms(deck["title"])
        scored = [(_topic_score(c["text"], terms), i, c) for i, c in enumerate(chunks)]
        top = min(len(chunks), max(needed, needed * 2))
        log.info("[job %s] triage: topic terms=%s, scores min=%.4f max=%.4f mean=%.4f",
                 job_id, sorted(terms)[:10],
                 min(t[0] for t in scored), max(t[0] for t in scored),
                 sum(t[0] for t in scored) / len(scored))
        best = sorted(scored, key=lambda t: (-t[0], t[1]))[:top]
        best = sorted(best, key=lambda t: t[1])
        step = len(best) / needed
        picked = [best[min(len(best) - 1, int(i * step))][2] for i in range(needed)]
        seen_idx = set()
        deduped = []
        for c in picked:
            if c["id"] not in seen_idx:
                seen_idx.add(c["id"])
                deduped.append(c)
        chunks = deduped
        log.info("[job %s] triage: sampled %s chunks from top-%s pool", job_id, len(chunks), top)

    try:
        provider, model = _get_provider(user_id)
        log.info("[job %s] provider=%s model=%s", job_id, provider.name, model)
        db.execute("UPDATE jobs SET status='running', total=?, started_at=datetime('now') WHERE id=?",
                   (len(chunks), job_id))
        seen_prompts: list[str] = []
        for chunk in chunks:
            log.info("[job %s] chunk %s: drafting (%s words)", job_id, chunk["idx"],
                     len(chunk["text"].split()))
            user_msg = (
                f"TOPIC: The user wants a quiz about: {deck['title']}. This text is one excerpt"
                f" from the source of truth for that topic.\n\n"
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
            for draft in drafts.questions:
                q = _normalize_options(draft)
                if any(_similarity(q.prompt, s) > 0.85 for s in seen_prompts):
                    log.info("[job %s] chunk %s: skipped duplicate prompt %r",
                             job_id, chunk["idx"], q.prompt[:80])
                    continue
                verify_msg = (
                    f"Source text:\n\"\"\"\n{chunk['text']}\n\"\"\"\n\n"
                    f"Question: {q.prompt}\nOptions: {json.dumps(q.options)}\n"
                    f"Claimed correct index: {q.correct_index}\n"
                    f"Explanation: {q.explanation}"
                )
                try:
                    verdict, _ = structured_call(provider, model, _system_prompt("verify"),
                                                 verify_msg, VerifyResponse, temperature=0.0,
                                                 max_tokens=512)
                    score = verdict.score if verdict.verdict == "pass" else 0.0
                    log.info("[job %s] chunk %s: verify verdict=%s score=%s",
                             job_id, chunk["idx"], verdict.verdict, score)
                except Exception as e:
                    log.warning("[job %s] chunk %s: verify call failed (%s: %s), assuming 0.5",
                                job_id, chunk["idx"], type(e).__name__, str(e)[:150])
                    score = 0.5
                if score < MIN_VERIFY_SCORE:
                    log.info("[job %s] chunk %s: rejected by verify (score=%s < %s): %r",
                             job_id, chunk["idx"], score, MIN_VERIFY_SCORE, q.prompt[:80])
                    continue
                inserted = db.query_one(
                    "SELECT COUNT(*) AS n FROM questions WHERE deck_id = ?", (deck_id,))["n"]
                if inserted >= target_questions:
                    db.execute("UPDATE jobs SET done=done+1, message=? WHERE id=?",
                               (f"Target reached ({target_questions} questions).", job_id))
                    break
                db.execute(
                    "INSERT INTO questions(deck_id, chunk_id, prompt, options_json, correct_index,"
                    " explanation, difficulty, model, prompt_version, verify_score)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (deck_id, chunk["id"], q.prompt, json.dumps(q.options), q.correct_index,
                     q.explanation, q.difficulty, model, PROMPT_VERSION, score),
                )
                seen_prompts.append(q.prompt)
            db.execute("UPDATE jobs SET done=done+1 WHERE id=?", (job_id,))
            time.sleep(0.05)
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


def start_generation(user_id: int, deck_id: int) -> str:
    import threading
    job_id = uuid.uuid4().hex
    db.execute("INSERT INTO jobs(id, user_id, deck_id) VALUES (?, ?, ?)", (job_id, user_id, deck_id))
    t = threading.Thread(target=generate_deck_questions, args=(user_id, deck_id, job_id), daemon=True)
    t.start()
    return job_id
