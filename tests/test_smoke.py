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
                    data={"question_id": q["id"], "choice": q["correct_index"]})
    assert r.status_code == 200 and "Easy" in r.text  # step 1: reveal + self-rating
    r = client.post(f"/study/{deck_id}/rate",
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


def test_levels_config():
    from app.config import LEVELS, DEFAULT_LEVEL
    assert DEFAULT_LEVEL in LEVELS
    for cfg in LEVELS.values():
        assert cfg["questions_per_chunk"] >= 1
        assert cfg["target_questions"] >= cfg["questions_per_chunk"]
        assert cfg["max_pages"] >= 1
        assert cfg["difficulty_hint"]


def test_challenge_page_detected():
    from app.ingest import _looks_like_challenge, IngestError, extract_text_from_url

    assert _looks_like_challenge("<html><body>Enable JavaScript and cookies to continue</body>")
    assert not _looks_like_challenge("<html><body>Ansible is an automation tool " + "x " * 300 + "</body>")


def test_url_challenge_raises(monkeypatch):
    from app import ingest

    def fake_fetch(url):
        return "<html><title>Just a moment...</title><body>Enable JavaScript and cookies to continue</body></html>"

    monkeypatch.setattr(ingest, "_fetch", fake_fetch)
    try:
        ingest.extract_text_from_url("https://example.com/")
        raise AssertionError("expected IngestError")
    except ingest.IngestError as e:
        assert str(e) == "__err_challenge"


def test_url_crawl_concatenates_pages(monkeypatch):
    from app import ingest

    page = ("<html><body>" + ("Ansible module content. " * 50) +
            '<a href="/page2">two</a></body></html>')
    page2 = "<html><body>" + ("Second page content. " * 50) + "</body></html>"

    def fake_fetch(url):
        return page if "page2" not in url else page2

    monkeypatch.setattr(ingest, "_fetch", fake_fetch)
    text = ingest.extract_text_from_url("https://example.com/", max_pages=5)
    assert "Ansible module content" in text
    assert "Second page content" in text


def test_new_source_saves_level():
    r = client.post("/new", data={"title": "Lv", "topic_text": "Kubernetes orchestrates containers. " * 60,
                                  "level": "thorough"}, follow_redirects=False)
    assert r.status_code == 303
    deck_id = int(r.headers["location"].split("/decks/")[1].split("?")[0])
    deck = db.query_one("SELECT * FROM decks WHERE id = ?", (deck_id,))
    assert deck["level"] == "thorough"

    r = client.get(f"/decks/{deck_id}/job-status")
    assert r.status_code == 200


def test_url_source_crawls_in_background(monkeypatch):
    import time
    from app import ingest

    client.post("/register", data={"email": "url1@b.c", "password": "password123",
                                   "display_name": "U1"}, follow_redirects=False)

    page = ("<html><body>" + ("Background crawl content. " * 50) +
            '<a href="/p2">two</a></body></html>')
    page2 = "<html><body>" + ("Crawled second page. " * 50) + "</body></html>"

    def fake_fetch(url):
        return page if "p2" not in url else page2

    monkeypatch.setattr(ingest, "_fetch", fake_fetch)

    r = client.post("/new", data={"title": "Url", "url": "https://example.com/",
                                  "level": "standard"}, follow_redirects=False)
    assert r.status_code == 303
    deck_id = int(r.headers["location"].split("/decks/")[1].split("?")[0])

    job = None
    for _ in range(50):
        job = db.query_one("SELECT * FROM jobs WHERE deck_id = ?", (deck_id,))
        if job and job["status"] in ("done", "failed"):
            break
        time.sleep(0.2)
    assert job["status"] == "done"
    src = db.query_one("SELECT raw_text FROM sources WHERE id = "
                       "(SELECT source_id FROM decks WHERE id = ?)", (deck_id,))
    assert "Background crawl content" in src["raw_text"]
    assert "Crawled second page" in src["raw_text"]


def test_url_source_challenge_fails_job(monkeypatch):
    import time
    from app import ingest

    client.post("/register", data={"email": "url2@b.c", "password": "password123",
                                   "display_name": "U2"}, follow_redirects=False)

    def fake_fetch(url):
        return "<html><title>Just a moment...</title><body>Enable JavaScript and cookies</body></html>"

    monkeypatch.setattr(ingest, "_fetch", fake_fetch)

    r = client.post("/new", data={"title": "Blk", "url": "https://example.com/",
                                  "level": "standard"}, follow_redirects=False)
    assert r.status_code == 303
    deck_id = int(r.headers["location"].split("/decks/")[1].split("?")[0])

    job = None
    for _ in range(50):
        job = db.query_one("SELECT * FROM jobs WHERE deck_id = ?", (deck_id,))
        if job and job["status"] in ("done", "failed"):
            break
        time.sleep(0.2)
    assert job["status"] == "failed"
    assert "blocks automated readers" in job["error"]
    deck = db.query_one("SELECT status FROM decks WHERE id = ?", (deck_id,))
    assert deck["status"] == "failed"
