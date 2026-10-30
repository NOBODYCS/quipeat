import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_tmp = tempfile.mkdtemp()
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("MASTER_KEY", "")
os.environ["LLM_MOCK"] = "true"
os.environ["DATABASE_PATH"] = str(Path(_tmp) / "test.db")
os.environ["UPLOAD_DIR"] = str(Path(_tmp) / "uploads")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app import db  # noqa: E402

db.init_db()
client = TestClient(app)


def test_register_login_and_full_flow():
    r = client.post("/register", data={"email": "a@b.c", "password": "password123",
                                       "display_name": "A"}, follow_redirects=False)
    assert r.status_code == 303 and "quipeat_session" in r.cookies

    r = client.get("/decks")
    assert r.status_code == 200 and "Your decks" in r.text

    r = client.post("/new", data={"title": "Bio", "topic_text": "Photosynthesis converts light into glucose. " * 60},
                    follow_redirects=False)
    assert r.status_code == 303, r.text[:500]
    deck_id = int(r.headers["location"].split("/decks/")[1].split("?")[0])

    import time
    for _ in range(50):
        job = db.query_one("SELECT * FROM jobs WHERE deck_id = ?", (deck_id,))
        if job and job["status"] in ("done", "failed"):
            break
        time.sleep(0.2)
    assert job["status"] == "done"
    qs = db.query("SELECT * FROM questions WHERE deck_id = ?", (deck_id,))
    assert len(qs) >= 2

    q = qs[0]
    r = client.post(f"/study/{deck_id}/answer",
                    data={"question_id": q["id"], "choice": q["correct_index"], "rating": "good"})
    assert r.status_code == 200 and "XP" in r.text

    user = db.query_one("SELECT * FROM users WHERE email = 'a@b.c'")
    assert user["xp_total"] > 0
    assert user["level"] >= 1

    r = client.get("/stats")
    assert r.status_code == 200
    r = client.get("/settings")
    assert r.status_code == 200


def test_key_storage_encrypted():
    from app.security import encrypt_secret, decrypt_secret
    enc = encrypt_secret("sk-test-1234567890abcdef")
    assert enc != "sk-test-1234567890abcdef"
    assert decrypt_secret(enc) == "sk-test-1234567890abcdef"


def test_chunking():
    from app.ingest import chunk_text
    text = "word " * 2000
    chunks = chunk_text(text)
    assert len(chunks) > 3
    assert all(len(c.split()) <= 450 for c in chunks)


def test_fsrs_monotonic_interval():
    from app.fsrs import grade
    card = {"ef": 2.5, "interval_days": 0, "reps": 0, "lapses": 0}
    for rating in ("good", "good", "easy"):
        card = {**card, **grade(card, rating)}
    assert card["interval_days"] >= 1
    card2 = {**card, **grade(card, "again")}
    assert card2["lapses"] == card["lapses"] + 1


def test_password_manager_friendly_login_form():
    r = client.get("/login")
    assert 'autocomplete="username"' in r.text
    assert 'autocomplete="current-password"' in r.text
    assert 'name="email"' in r.text and 'name="password"' in r.text
