"""Build a topic pack through the app's own generation pipeline.

Usage:
  LLM_MOCK=true python scripts/build_pack.py --topic "Photosynthesis" --count 100 --lang en --out packs/photosynthesis.pack.json
  python scripts/build_pack.py --topic "..." --count 100 --lang en   # with a real API key

The pack is generated exactly like a topic-mode deck: research document ->
chunking -> generate (v2 prompts) -> batch verify -> dedup. Questions that fail
verification are dropped, so a small mock run may yield fewer than --count.
"""
import argparse
import json
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.generation import generate_deck_questions  # noqa: E402
from app.ingest import chunk_text_v2  # noqa: E402
from app.packs import PACK_FORMAT, validate_pack  # noqa: E402


def build(topic: str, count: int, lang: str, out_path: Path) -> int:
    with tempfile.TemporaryDirectory():
        settings.database_path = str(Path(tempfile.mkdtemp()) / "pack.db")
        db.init_db()
        cur = db.execute("INSERT INTO users(email, password_hash) VALUES (?, ?)",
                         (f"pack-{uuid.uuid4().hex[:8]}@local", "x"))
        user_id = cur.lastrowid
        db.execute("INSERT INTO user_settings(user_id) VALUES (?)", (user_id,))
        cur = db.execute("INSERT INTO sources(user_id, kind, title, original_ref, raw_text) VALUES (?, 'topic', ?, ?, '')",
                         (user_id, topic, topic))
        source_id = cur.lastrowid
        cur = db.execute(
            "INSERT INTO decks(user_id, source_id, title, goal, lang, level, target_questions, status)"
            " VALUES (?, ?, ?, ?, ?, 'standard', ?, 'draft')",
            (user_id, source_id, topic, f"Master: {topic}", lang, count))
        deck_id = cur.lastrowid
        job_id = "pack-" + uuid.uuid4().hex[:8]
        db.execute("INSERT INTO jobs(id, user_id, deck_id) VALUES (?, ?, ?)", (job_id, user_id, deck_id))
        generate_deck_questions(user_id, deck_id, job_id)
        job = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if job["status"] == "failed":
            print(f"generation failed: {job['error']}", file=sys.stderr)
            return 1
        qs = db.query("SELECT * FROM questions WHERE deck_id = ? ORDER BY id", (deck_id,))
        deck = db.query_one("SELECT * FROM decks WHERE id = ?", (deck_id,))
        pack = {
            "format": PACK_FORMAT,
            "title": topic,
            "lang": lang,
            "questions": [
                {"prompt": q["prompt"], "options": json.loads(q["options_json"]),
                 "correct_index": q["correct_index"], "explanation": q["explanation"],
                 "difficulty": q["difficulty"]}
                for q in qs
            ],
        }
        validate_pack(pack)  # self-check
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[OK] {out_path}: {len(pack['questions'])} questions (target {count}), "
              f"job: {job['message']}")
        return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--lang", default="en", choices=["en", "pl"])
    ap.add_argument("--out", default=None, help="default: packs/<slug>.pack.json")
    args = ap.parse_args()
    out = Path(args.out) if args.out else Path("packs") / (args.topic.lower().replace(" ", "_")[:40] + ".pack.json")
    sys.exit(build(args.topic, args.count, args.lang, out))
