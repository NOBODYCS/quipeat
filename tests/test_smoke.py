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
from app import db, ingest  # noqa: E402

# Keep tests hermetic: never launch a real browser via Crawl4AI.
# Tests that exercise the Crawl4AI path monkeypatch it explicitly.
ingest._CRAWL4AI_AVAILABLE = False

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
    deck_id = int(r.headers["location"].split("created=")[1])

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
    assert r.status_code == 200 and "Good" in r.text  # result + continue buttons
    assert "XP" in r.text and "flag" in r.text  # xp preview + inline flag control
    r = client.post(f"/study/{deck_id}/rate",
                    data={"question_id": q["id"], "choice": q["correct_index"], "rating": "good"})
    assert r.status_code == 200  # grades + returns next card (or session end redirect)

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
    deck_id = int(r.headers["location"].split("created=")[1])
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
    deck_id = int(r.headers["location"].split("created=")[1])

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
    deck_id = int(r.headers["location"].split("created=")[1])

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


def test_url_extraction_prefers_crawl4ai(monkeypatch):
    from app import ingest

    calls = []

    def fake_c4a(url, max_pages):
        calls.append((url, max_pages))
        return "## Rendered markdown heading\n\n" + "Playwright-rendered content. " * 30

    monkeypatch.setattr(ingest, "_CRAWL4AI_AVAILABLE", True)
    monkeypatch.setattr(ingest, "_crawl4ai_extract", fake_c4a)

    text = ingest.extract_text_from_url("https://example.com/docs/", max_pages=8)
    assert "Playwright-rendered content" in text
    assert calls == [("https://example.com/docs/", 8)]


def test_url_extraction_falls_back_on_crawl4ai_error(monkeypatch):
    from app import ingest

    def broken_c4a(url, max_pages):
        raise RuntimeError("browser missing")

    monkeypatch.setattr(ingest, "_CRAWL4AI_AVAILABLE", True)
    monkeypatch.setattr(ingest, "_crawl4ai_extract", broken_c4a)

    page = "<html><body>" + ("Fallback trafilatura content. " * 50) + "</body></html>"

    def fake_fetch(url):
        return page

    monkeypatch.setattr(ingest, "_fetch", fake_fetch)
    text = ingest.extract_text_from_url("https://example.com/")
    assert "Fallback trafilatura content" in text


def test_url_extraction_crawl4ai_challenge_raises(monkeypatch):
    from app import ingest

    def challenge_c4a(url, max_pages):
        raise ingest.IngestError("__err_challenge")

    monkeypatch.setattr(ingest, "_CRAWL4AI_AVAILABLE", True)
    monkeypatch.setattr(ingest, "_crawl4ai_extract", challenge_c4a)

    try:
        ingest.extract_text_from_url("https://example.com/")
        raise AssertionError("expected IngestError")
    except ingest.IngestError as e:
        assert str(e) == "__err_challenge"


def test_generation_prompt_is_topic_aware():
    """The LLM must receive the deck title as TOPIC so it can judge relevance."""
    import time

    from app.llm.mock import MockProvider

    sent = {}

    class SpyProvider(MockProvider):
        def chat(self, model, system, user, **kw):
            sent.setdefault("system", system)
            sent.setdefault("user", user)
            return super().chat(model, system, user, **kw)

    from app import generation
    orig = generation._get_provider
    generation._get_provider = lambda user_id: (SpyProvider(), "mock-model")
    try:
        r = client.post("/new", data={"title": "Ansible fundamentals",
                                      "topic_text": "Ansible playbooks run tasks in order. " * 60},
                        follow_redirects=False)
        assert r.status_code == 303
        deck_id = int(r.headers["location"].split("created=")[1])
        job = None
        for _ in range(50):
            job = db.query_one("SELECT * FROM jobs WHERE deck_id = ?", (deck_id,))
            if job and job["status"] in ("done", "failed"):
                break
            time.sleep(0.2)
        assert job["status"] == "done"
        assert "TOPIC: The user wants a quiz about: Ansible fundamentals" in sent["user"]
        assert "Topic relevance" in sent["system"]
    finally:
        generation._get_provider = orig


def test_topic_triage_prefers_relevant_chunks():
    """When the source exceeds the budget, sampling must prefer on-topic chunks."""
    from app.generation import _topic_score, _topic_terms

    terms = _topic_terms("Ansible quiz fundamentals")
    assert "ansible" in terms
    assert "quiz" not in terms and "fundamentals" not in terms

    relevant = "Ansible playbooks use YAML to define tasks and modules " * 10
    filler = "Please contribute by opening a pull request on GitHub " * 10
    assert _topic_score(relevant, terms) > _topic_score(filler, terms) * 3
    assert _topic_score(filler, terms) >= 0.0


def test_triaged_sampling_end_to_end():
    """A mixed source must spend the question budget on the relevant part."""
    import time

    relevant = ("Ansible playbooks use YAML to define tasks, modules, and inventory hosts. "
                 "The ansible-playbook command runs them in order. ") * 20
    filler = ("Copyright Red Hat. Contributing guide: open a pull request. "
              "License: GPLv3. Changelog for release 2.15: minor fixes. ") * 20

    big_text = (relevant + " " + filler) * 40
    r = client.post("/new", data={"title": "Ansible", "topic_text": big_text,
                                  "level": "quick"}, follow_redirects=False)
    assert r.status_code == 303
    deck_id = int(r.headers["location"].split("created=")[1])

    job = None
    for _ in range(80):
        job = db.query_one("SELECT * FROM jobs WHERE deck_id = ?", (deck_id,))
        if job and job["status"] in ("done", "failed"):
            break
        time.sleep(0.2)
    assert job["status"] == "done"

    chunks_used = db.query(
        "SELECT c.text FROM questions q JOIN chunks c ON c.id = q.chunk_id WHERE q.deck_id = ?",
        (deck_id,))
    assert chunks_used, "expected questions to be generated"
    relevant_hits = sum(1 for c in chunks_used if "playbook" in c["text"] or "ansible" in c["text"].lower())
    assert relevant_hits == len(chunks_used)


def test_topic_triage_handles_unicode_titles():
    """Diacritics in the deck title (e.g. Polish) must survive tokenization."""
    from app.generation import _topic_score, _topic_terms

    terms = _topic_terms("Quiz o konfiguracji Ansible")
    assert "konfiguracji" in terms and "ansible" in terms
    assert "quiz" not in terms

    pl_text = "Konfiguracja playbooków w Ansible opiera się na plikach YAML " * 10
    en_filler = "Please contribute by opening a pull request on GitHub " * 10
    assert _topic_score(pl_text, terms) > _topic_score(en_filler, terms)


def test_mistral_provider_sends_named_json_schema(monkeypatch):
    """Mistral's API rejects response_format.json_schema without a name (422)."""
    from app.llm.providers import MistralProvider

    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "{}"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    def fake_post(url, headers=None, json=None):
        captured["payload"] = json
        return FakeResp()

    monkeypatch.setattr("app.llm.providers.httpx.Client", lambda **kw: type(
        "C", (), {"__enter__": lambda s: s, "__exit__": lambda s, *a: None,
                  "post": lambda self, url, **kw: fake_post(url, **kw)})())
    p = MistralProvider("test-key")
    p.chat("mistral-large-latest", "sys", "user", json_schema={"type": "object"})
    rf = captured["payload"]["response_format"]
    assert rf["json_schema"]["name"] == "response"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"] == {"type": "object"}
