"""Topic pack library: versioned JSON pack format, export/import, library listing.

Pack format (format=1):
{
  "format": 1,
  "title": "...",
  "lang": "en",
  "questions": [{"prompt", "options": [4], "correct_index", "explanation", "difficulty"}, ...]
}
"""
import json
import re
from pathlib import Path

from typing import Literal

from pydantic import BaseModel, Field, ValidationError

PACK_FORMAT = 1
PACKS_DIR = Path(__file__).parent.parent / "packs"


class PackQuestion(BaseModel):
    prompt: str = Field(min_length=10, max_length=400)
    options: list[str] = Field(min_length=4, max_length=4)
    correct_index: int = Field(ge=0, le=3)
    explanation: str = Field(min_length=10, max_length=600)
    difficulty: str = "medium"


class Pack(BaseModel):
    format: Literal[1] = PACK_FORMAT
    title: str = Field(min_length=2, max_length=120)
    lang: str = Field(pattern=r"^(en|pl)$")
    questions: list[PackQuestion] = Field(min_length=1)


def validate_pack(data: dict) -> Pack:
    """Parse + validate a pack dict. Raises ValueError with a readable message."""
    try:
        pack = Pack.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"invalid pack: {e.errors()[0]['msg']} at {e.errors()[0]['loc']}")
    for i, q in enumerate(pack.questions):
        opts = [o.strip().lower() for o in q.options if o.strip()]
        if len(set(opts)) != 4:
            raise ValueError(f"question {i}: options must be 4 distinct non-empty strings")
        if any("Distractor" in o for o in q.options):
            raise ValueError(f"question {i}: placeholder option")
        if re.search(r"\b(according to the (text|passage|author|chapter))\b", q.prompt, re.I):
            raise ValueError(f"question {i}: meta reference in prompt")
    return pack


def export_deck_pack(deck, questions: list) -> dict:
    return {
        "format": PACK_FORMAT,
        "title": deck["title"],
        "lang": deck["lang"] if "lang" in deck.keys() else "en",
        "questions": [
            {"prompt": q["prompt"], "options": json.loads(q["options_json"]),
             "correct_index": q["correct_index"], "explanation": q["explanation"],
             "difficulty": q["difficulty"]}
            for q in questions
        ],
    }


def import_pack_into_deck(user_id: int, pack: Pack) -> int:
    """Create a new deck owned by user_id from a validated pack, with fresh
    FSRS state and origin='pack' on every question. Returns deck_id."""
    from . import db
    from .ingest import chunk_text_v2
    raw = "\n\n".join(f"Q: {q.prompt}\n{chr(10).join(q.options)}\nA: {q.options[q.correct_index]}\n{q.explanation}"
                      for q in pack.questions)
    cur = db.execute("INSERT INTO sources(user_id, kind, title, original_ref, raw_text) VALUES (?, 'pack', ?, ?, ?)",
                     (user_id, pack.title, pack.title, raw))
    source_id = cur.lastrowid
    for idx, piece in enumerate(chunk_text_v2(raw)):
        db.execute("INSERT INTO chunks(source_id, idx, text) VALUES (?, ?, ?)", (source_id, idx, piece))
    cur = db.execute(
        "INSERT INTO decks(user_id, source_id, title, lang, status) VALUES (?, ?, ?, ?, 'ready')",
        (user_id, source_id, pack.title, pack.lang))
    deck_id = cur.lastrowid
    for q in pack.questions:
        db.execute(
            "INSERT INTO questions(deck_id, prompt, options_json, correct_index, explanation,"
            " difficulty, model, prompt_version, origin) VALUES (?, ?, ?, ?, ?, ?, 'pack', 'pack-v1', 'pack')",
            (deck_id, q.prompt, json.dumps(q.options), q.correct_index, q.explanation, q.difficulty))
    return deck_id


def list_shipped_packs() -> list[dict]:
    """Scan packs/ for *.pack.json files; return [{file, title, lang, count, valid, error}]."""
    out = []
    if not PACKS_DIR.exists():
        return out
    for f in sorted(PACKS_DIR.glob("*.pack.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            pack = validate_pack(data)
            out.append({"file": f.name, "title": pack.title, "lang": pack.lang,
                        "count": len(pack.questions), "valid": True, "error": ""})
        except Exception as e:
            out.append({"file": f.name, "title": f.stem, "lang": "?", "count": 0,
                        "valid": False, "error": str(e)[:120]})
    return out


def load_pack_file(name: str) -> Pack:
    """Load a shipped pack by file name (no path traversal)."""
    safe = Path(name).name
    path = PACKS_DIR / safe
    if not path.exists() or path.parent.resolve() != PACKS_DIR.resolve():
        raise ValueError("pack not found")
    return validate_pack(json.loads(path.read_text(encoding="utf-8")))
