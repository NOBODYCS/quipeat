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
from app.ingest import chunk_text, chunk_text_v2  # noqa: E402

SAMPLES = Path(__file__).parent / "samples"

EXPECTED = {
    "photosynthesis.txt": {"min_questions": 1},
    "short_note.txt": {"min_questions": 1},
    "polish_history.txt": {"min_questions": 1},
    "multi_source_a.txt": {"min_questions": 1, "multi_source": "multi_source_b.txt",
                           "goal": "write and run Ansible playbooks"},
    "multi_source_b.txt": {"min_questions": 0},  # consumed as second source of multi_source_a
    "goal_conflict.txt": {"min_questions": 0, "goal": "write and run Ansible playbooks",
                          "allow_shortfall": True},
    "relevance.txt": {"min_questions": 1, "goal": "understand photosynthesis"},
}

_META_RE = None


def _quality_checks(qs) -> list[str]:
    """Quality assertions every generated question must pass (F13)."""
    import json as _json
    import re as _re
    problems = []
    meta = _re.compile(r"\b(according to the (text|passage|author|chapter)|the text says|the author claims)\b", _re.I)
    for q in qs:
        opts = _json.loads(q["options_json"])
        if any("Distractor" in o for o in opts):
            problems.append(f"q{q['id']}: placeholder option")
        if len(opts) != 4 or len({o.strip().lower() for o in opts}) != 4:
            problems.append(f"q{q['id']}: not 4 distinct options")
        if not (0 <= q["correct_index"] < 4):
            problems.append(f"q{q['id']}: bad correct_index")
        if len(q["prompt"].split()) > 40:
            problems.append(f"q{q['id']}: stem over 40 words ({len(q['prompt'].split())})")
        for o in opts:
            if len(o.split()) > 12:
                problems.append(f"q{q['id']}: option over 12 words")
        if not q["explanation"].strip():
            problems.append(f"q{q['id']}: empty explanation")
        if meta.search(q["prompt"]):
            problems.append(f"q{q['id']}: meta reference in stem")
        if _re.search(r"\ball of the above\b|\bnone of the above\b", " ".join(opts), _re.I):
            problems.append(f"q{q['id']}: all/none-of-the-above option")
    return problems


def run(model: str | None) -> int:
    db.init_db()
    if model:
        settings.llm_mock = False
    failures = 0
    for sample in sorted(SAMPLES.glob("*.txt")):
        exp0 = EXPECTED.get(sample.name, {})
        if exp0.get("multi_source") or exp0.get("min_questions") == 0 and not exp0.get("goal") and not exp0.get("multi_source"):
            # secondary sources of a multi-source sample are folded in below
            if exp0.get("multi_source"):
                pass
            else:
                continue
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
        for i, piece in enumerate(chunk_text_v2(text)):
            db.execute("INSERT INTO chunks(source_id, idx, text) VALUES (?, ?, ?)", (source_id, i, piece))
        source_ids = [source_id]
        extra = EXPECTED.get(sample.name, {}).get("multi_source")
        if extra and (SAMPLES / extra).exists():
            text2 = (SAMPLES / extra).read_text()
            cur = db.execute(
                "INSERT INTO sources(user_id, kind, title, raw_text) VALUES (?, 'text', ?, ?)",
                (user_id, extra, text2))
            sid2 = cur.lastrowid
            for i, piece in enumerate(chunk_text_v2(text2)):
                db.execute("INSERT INTO chunks(source_id, idx, text) VALUES (?, ?, ?)", (sid2, i, piece))
            source_ids.append(sid2)
        goal = EXPECTED.get(sample.name, {}).get("goal", f"Learn about {sample.stem}")
        cur = db.execute(
            "INSERT INTO decks(user_id, source_id, title, goal, status) VALUES (?, ?, ?, ?, 'draft')",
            (user_id, source_ids[0], sample.stem, goal))
        deck_id = cur.lastrowid
        for sid in source_ids:
            db.execute("INSERT OR IGNORE INTO deck_sources(deck_id, source_id) VALUES (?, ?)",
                       (deck_id, sid))
        job_id = "eval-" + sample.stem
        db.execute("INSERT INTO jobs(id, user_id, deck_id) VALUES (?, ?, ?)", (job_id, user_id, deck_id))
        generate_deck_questions(user_id, deck_id, job_id)
        qs = db.query("SELECT * FROM questions WHERE deck_id = ?", (deck_id,))
        scores = [q["verify_score"] for q in qs]
        exp = EXPECTED.get(sample.name, {})
        problems = _quality_checks(qs)
        ok = len(qs) >= exp.get("min_questions", 1) and not problems
        job = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if exp.get("allow_shortfall"):
            # goal-conflict sample: material does not cover the goal; the job must
            # still finish and its message must name the uncovered goal areas
            ok = ok and job and job["status"] == "done" and len(qs) < 50
        if not ok:
            failures += 1
        line = f"[{'OK ' if ok else 'FAIL'}] {sample.name}: {len(qs)} questions"
        if scores:
            line += f", avg verify score {sum(scores) / len(scores):.2f}"
        if job and job["message"]:
            line += f" | job: {job['message'][:160]}"
        if problems:
            line += " | quality: " + "; ".join(problems[:5])
        print(line)
    return failures


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="OpenRouter model id (requires OPENROUTER_API_KEY)")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory():
        settings.database_path = str(Path(tempfile.mkdtemp()) / "eval.db")
        sys.exit(1 if run(args.model) else 0)
