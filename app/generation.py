import json
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

PROMPT_VERSION = "v1"
MIN_VERIFY_SCORE = 0.7

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
    deck = db.query_one("SELECT * FROM decks WHERE id = ?", (deck_id,))
    gen_lang = deck["lang"] if "lang" in deck.keys() and deck["lang"] in LANG_NAMES else "en"
    level = deck["level"] if "level" in deck.keys() and deck["level"] in LEVELS else DEFAULT_LEVEL
    cfg = LEVELS[level]
    questions_per_chunk = cfg["questions_per_chunk"]
    max_questions = cfg["max_questions"]
    chunks = db.query(
        "SELECT * FROM chunks WHERE source_id = ? ORDER BY idx", (deck["source_id"],))
    try:
        provider, model = _get_provider(user_id)
        db.execute("UPDATE jobs SET status='running', total=?, started_at=datetime('now') WHERE id=?",
                   (len(chunks), job_id))
        seen_prompts: list[str] = []
        for chunk in chunks:
            user_msg = (
                f"Source text:\n\"\"\"\n{chunk['text']}\n\"\"\"\n\n"
                f"Generate {questions_per_chunk} multiple-choice questions from the source text above."
                + cfg["difficulty_hint"] + _lang_instruction(gen_lang)
            )
            try:
                drafts, _ = structured_call(provider, model, _system_prompt("generate"),
                                             user_msg, GenerateResponse, temperature=0.2,
                                             max_tokens=2048)
            except Exception as e:
                db.execute("UPDATE jobs SET done=done+1, message=? WHERE id=?",
                           (f"chunk {chunk['idx']}: generation failed: {str(e)[:200]}", job_id))
                continue
            for draft in drafts.questions:
                q = _normalize_options(draft)
                if any(_similarity(q.prompt, s) > 0.85 for s in seen_prompts):
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
                except Exception:
                    score = 0.5
                if score < MIN_VERIFY_SCORE:
                    continue
                inserted = db.query_one(
                    "SELECT COUNT(*) AS n FROM questions WHERE deck_id = ?", (deck_id,))["n"]
                if inserted >= max_questions:
                    db.execute("UPDATE jobs SET done=done+1, message=? WHERE id=?",
                               (f"Question cap reached ({max_questions}); skipping remaining chunks.", job_id))
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
        db.execute("UPDATE jobs SET status='done', finished_at=datetime('now'), message='Generation complete' WHERE id=?",
                   (job_id,))
        db.execute("UPDATE decks SET status='ready' WHERE id=?", (deck_id,))
    except Exception as e:
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
