"""Eval harness for question generation quality.

Usage:
  LLM_MOCK=true python scripts/eval_generation.py            # sanity run, no key needed
  python scripts/eval_generation.py --model mistralai/mistral-large-latest

Runs the sample docs in scripts/samples/ through the full generate->verify->dedup
pipeline and prints per-deck stats. Extend EXPECTED to hand-check answers.
"""
import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.generation import generate_deck_questions  # noqa: E402
from app.ingest import chunk_text  # noqa: E402

SAMPLES = Path(__file__).parent / "samples"

EXPECTED = {
    "photosynthesis.txt": {"min_questions": 1},
}


def run(model: str | None) -> int:
    db.init_db()
    if model:
        settings.llm_mock = False
    failures = 0
    for sample in sorted(SAMPLES.glob("*.txt")):
        text = sample.read_text()
        cur = db.execute(
            "INSERT INTO users(email, password_hash) VALUES (?, ?)",
            (f"eval-{sample.stem}@local", "x"))
        user_id = cur.lastrowid
        db.execute("INSERT INTO user_settings(user_id) VALUES (?)", (user_id,))
        if model:
            from app.security import encrypt_secret
            cur = db.execute(
                "INSERT INTO api_keys(user_id, provider, label, encrypted_key) VALUES (?, 'openrouter', 'eval', ?)",
                (user_id, encrypt_secret(__import__("os").environ["OPENROUTER_API_KEY"])))
            db.execute("UPDATE user_settings SET active_key_id=?, default_model=? WHERE user_id=?",
                       (cur.lastrowid, model, user_id))
        cur = db.execute(
            "INSERT INTO sources(user_id, kind, title, raw_text) VALUES (?, 'text', ?, ?)",
            (user_id, sample.stem, text))
        source_id = cur.lastrowid
        for i, piece in enumerate(chunk_text(text)):
            db.execute("INSERT INTO chunks(source_id, idx, text) VALUES (?, ?, ?)", (source_id, i, piece))
        cur = db.execute("INSERT INTO decks(user_id, source_id, title, status) VALUES (?, ?, ?, 'draft')",
                         (user_id, source_id, sample.stem))
        deck_id = cur.lastrowid
        job_id = "eval-" + sample.stem
        db.execute("INSERT INTO jobs(id, user_id, deck_id) VALUES (?, ?, ?)", (job_id, user_id, deck_id))
        generate_deck_questions(user_id, deck_id, job_id)
        qs = db.query("SELECT * FROM questions WHERE deck_id = ?", (deck_id,))
        scores = [q["verify_score"] for q in qs]
        exp = EXPECTED.get(sample.name, {})
        ok = len(qs) >= exp.get("min_questions", 1)
        if not ok:
            failures += 1
        print(f"[{'OK ' if ok else 'FAIL'}] {sample.name}: {len(qs)} questions, "
              f"avg verify score {sum(scores) / len(scores):.2f}" if scores else
              f"[FAIL] {sample.name}: 0 questions")
    return failures


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="OpenRouter model id (requires OPENROUTER_API_KEY)")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory():
        settings.database_path = str(Path(tempfile.mkdtemp()) / "eval.db")
        sys.exit(1 if run(args.model) else 0)
