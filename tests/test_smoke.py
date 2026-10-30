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


def csrf_post(c, url, data=None, files=None, **kw):
    """POST with the session CSRF token (mimics the hidden form input)."""
    import re as _re
    page = c.get("/decks")
    m = _re.search(r'name="csrf-token" content="([0-9a-f]+)"', page.text)
    tok = m.group(1) if m else ""
    data = dict(data or {})
    data["csrf"] = tok
    if files is not None:
        return c.post(url, data=data, files=files, **kw)
    kw.setdefault("follow_redirects", False)
    return c.post(url, data=data, **kw)



def test_register_login_and_full_flow():
    r = client.post("/register", data={"email": "a@b.c", "password": "password123",
                                       "display_name": "A"}, follow_redirects=False)
    assert r.status_code == 303 and "quipeat_session" in r.cookies

    r = client.get("/decks")
    assert r.status_code == 200 and "Your decks" in r.text

    r = csrf_post(client, "/new", data={"title": "Bio", "goal": "Understand photosynthesis basics", "topic_text": "Photosynthesis converts light into glucose. " * 60},
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
    r = csrf_post(client, f"/study/{deck_id}/answer",
                    data={"question_id": q["id"], "choice": q["correct_index"]})
    assert r.status_code == 200 and "Good" in r.text  # result + continue buttons
    assert "XP" in r.text and "flag" in r.text  # xp preview + inline flag control
    r = csrf_post(client, f"/study/{deck_id}/rate",
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
    r = csrf_post(client, "/new", data={"title": "Lv", "goal": "Learn Kubernetes orchestration", "topic_text": "Kubernetes orchestrates containers. " * 60,
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

    r = csrf_post(client, "/new", data={"title": "Url", "goal": "Learn background crawling", "url": "https://example.com/",
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

    r = csrf_post(client, "/new", data={"title": "Blk", "goal": "Learn blocked pages handling", "url": "https://example.com/",
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
            if "quiz writer" in system or "expert quiz writer" in system:
                sent["system"] = system
                sent["user"] = user
            return super().chat(model, system, user, **kw)

    client.post("/register", data={"email": "spy@b.c", "password": "password123",
                                   "display_name": "S"}, follow_redirects=False)
    from app import generation
    orig = generation._get_provider
    generation._get_provider = lambda user_id: (SpyProvider(), "mock-model")
    try:
        r = csrf_post(client, "/new", data={"title": "Ansible fundamentals", "goal": "Run Ansible playbooks",
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
        assert "LEARNING GOAL: The user wants to learn: Run Ansible playbooks" in sent["user"]
        assert "quiz writer" in sent["system"]
        assert "Wording rules" in sent["system"]
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
    r = csrf_post(client, "/new", data={"title": "Ansible", "goal": "Use Ansible for automation", "topic_text": big_text,
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
        status_code = 200

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


def test_provider_retries_transient_errors(monkeypatch):
    """503 from the provider must be retried, then succeed."""
    from app.llm import providers

    calls = {"n": 0}

    class Resp:
        def __init__(self, status):
            self.status_code = status
            self.text = ""

        def raise_for_status(self):
            if self.status_code >= 400:
                import httpx
                raise httpx.HTTPStatusError("err", request=None, response=self)

        def json(self):
            return {"choices": [{"message": {"content": "{}"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    def fake_post(**kw):
        calls["n"] += 1
        return Resp(503 if calls["n"] < 3 else 200)

    monkeypatch.setattr(providers.time, "sleep", lambda s: None)

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kw):
            return fake_post(**kw)

    monkeypatch.setattr(providers.httpx, "Client", FakeClient)
    p = providers.MistralProvider("test-key")
    result = p.chat("m", "sys", "user", json_schema={"type": "object"})
    assert calls["n"] == 3
    assert result.text == "{}"


def test_semantic_triage_drops_non_learning_chunks(monkeypatch):
    """Contributor/changelog content must be dropped for ANY source type —
    the gate judges content, not URL patterns."""
    import time
    from app import generation

    def fake_triage(provider, model, title, chunks, job_id):
        kept = [c for c in chunks if "playbook" in c["text"] or "inventory" in c["text"]]
        return kept

    monkeypatch.setattr(generation, "_semantic_triage", fake_triage)

    mixed = ("Ansible playbooks define tasks in YAML against an inventory. "
             "To contribute a new module, open a pull request following the contributor "
             "path and changelog fragment process. ") * 40
    r = csrf_post(client, "/new", data={"title": "Ansible", "goal": "Use Ansible playbooks", "topic_text": mixed,
                                  "level": "quick"}, follow_redirects=False)
    assert r.status_code == 303
    deck_id = int(r.headers["location"].split("created=")[1])
    job = None
    for _ in range(60):
        job = db.query_one("SELECT * FROM jobs WHERE deck_id = ?", (deck_id,))
        if job and job["status"] in ("done", "failed"):
            break
        time.sleep(0.2)
    assert job["status"] == "done"

    qs = db.query("SELECT q.prompt FROM questions q WHERE q.deck_id = ?", (deck_id,))
    assert qs, "expected questions from learning chunks"
    for q in qs:
        assert "contribut" not in q["prompt"].lower()
        assert "changelog" not in q["prompt"].lower()


def test_goal_is_required_and_persisted():
    client.post("/register", data={"email": "goal1@b.c", "password": "password123",
                                   "display_name": "G1"}, follow_redirects=False)
    r = csrf_post(client, "/new", data={"title": "NoGoal", "topic_text": "Some content. " * 60},
                    follow_redirects=False)
    assert r.status_code == 200
    assert "what you want to learn" in r.text.lower()

    r = csrf_post(client, "/new", data={"title": "WithGoal", "goal": "Master inventory basics",
                                  "topic_text": "Ansible inventory hosts and groups. " * 60},
                    follow_redirects=False)
    assert r.status_code == 303
    deck_id = int(r.headers["location"].split("created=")[1])
    deck = db.query_one("SELECT goal FROM decks WHERE id = ?", (deck_id,))
    assert deck["goal"] == "Master inventory basics"


def test_all_registered_prompts_exist_and_load():
    """A prompt referenced in _PROMPTS but missing on disk fails OPEN in
    production (triage KeyError bug). It must fail loudly instead."""
    from app.generation import _PROMPTS, _system_prompt
    assert set(_PROMPTS) == {"generate", "verify", "triage", "research"}
    for name, path in _PROMPTS.items():
        assert path.exists(), f"prompt file missing: {path}"
        text = _system_prompt(name)
        assert len(text) > 200, f"prompt {name} looks empty"


def test_triage_prompt_is_sent_to_lld(monkeypatch):
    """The triage call must actually use the registered triage system prompt."""
    import time
    from app.llm.mock import MockProvider

    sent = {}

    class SpyProvider(MockProvider):
        def chat(self, model, system, user, **kw):
            sent.setdefault("systems", []).append(system[:40])
            return super().chat(model, system, user, **kw)

    from app import generation
    orig = generation._get_provider
    generation._get_provider = lambda user_id: (SpyProvider(), "mock-model")
    try:
        client.post("/register", data={"email": "triage@b.c", "password": "password123",
                                       "display_name": "T"}, follow_redirects=False)
        r = csrf_post(client, "/new", data={"title": "T", "goal": "Use Ansible playbooks",
                                      "topic_text": "Ansible playbooks run tasks. " * 400},
                        follow_redirects=False)
        assert r.status_code == 303
        deck_id = int(r.headers["location"].split("created=")[1])
        for _ in range(50):
            job = db.query_one("SELECT * FROM jobs WHERE deck_id = ?", (deck_id,))
            if job and job["status"] in ("done", "failed"):
                break
            time.sleep(0.2)
        assert job["status"] == "done"
        assert any("classify excerpts" in sys.lower() or "learning material" in sys for sys in sent["systems"]), sent["systems"]
    finally:
        generation._get_provider = orig


def test_admin_panel_shows_diagnostics_and_job_events():
    """Admin observability: component health, recent jobs, and per-job events."""
    import time

    client.post("/register", data={"email": "admintest@b.c", "password": "password123",
                                   "display_name": "AT"}, follow_redirects=False)
    db.execute("UPDATE users SET role='admin' WHERE email='admintest@b.c'")

    r = csrf_post(client, "/new", data={"title": "Obs", "goal": "Learn observability",
                                  "topic_text": "Observability needs logs metrics traces. " * 60},
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

    events = db.query("SELECT * FROM job_events WHERE job_id = ?", (job["id"],))
    assert events, "expected job events to be persisted"
    assert any("deck=" in e["message"] for e in events)

    r = client.get("/admin")
    assert r.status_code == 200
    assert "System diagnostics" in r.text
    assert "Recent generation jobs" in r.text
    assert "events" in r.text

    r = client.get(f"/admin/jobs/{job['id']}/events")
    assert r.status_code == 200
    assert "chunks available" in r.text

    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "degraded")
    assert "database" in body["components"]


def test_file_upload_and_topic_decks_generate():
    """File upload and pasted-topic paths must both create working decks."""
    import io
    import time

    client.post("/register", data={"email": "srctest@b.c", "password": "password123",
                                   "display_name": "ST"}, follow_redirects=False)

    files = {"file": ("notes.txt", io.BytesIO(
        b"Ansible playbooks automate server configuration with YAML tasks. " * 60), "text/plain")}
    r = csrf_post(client, "/new", data={"title": "FileDeck", "goal": "Learn file ingestion",
                                   "level": "quick"}, files=files, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    file_deck = int(r.headers["location"].split("created=")[1])

    r = csrf_post(client, "/new", data={"title": "TopicDeck", "goal": "Learn topic ingestion",
                                  "topic_text": "Kubernetes pods are scheduled onto nodes. " * 80},
                    follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    topic_deck = int(r.headers["location"].split("created=")[1])

    outcomes = {}
    for deck_id in (file_deck, topic_deck):
        job = None
        for _ in range(60):
            job = db.query_one("SELECT * FROM jobs WHERE deck_id = ?", (deck_id,))
            if job and job["status"] in ("done", "failed"):
                break
            time.sleep(0.2)
        assert job["status"] == "done", (job["status"], job["error"])
        qs = db.query_one("SELECT COUNT(*) AS n FROM questions WHERE deck_id = ?", (deck_id,))
        outcomes[deck_id] = qs["n"]
        assert qs["n"] > 0, f"deck {deck_id} generated no questions"
        deck = db.query_one("SELECT status FROM decks WHERE id = ?", (deck_id,))
        assert deck["status"] == "ready"
        assert not job["error"]


def test_job_events_pruned_and_regenerate_text_deck():
    """Storage stays bounded: retention pruning works, and regenerating a
    text deck (no crawler) re-chunks from stored raw_text instead of ending
    up with zero chunks and zero questions."""
    import time

    client.post("/register", data={"email": "prune@b.c", "password": "password123",
                                   "display_name": "P"}, follow_redirects=False)

    r = csrf_post(client, "/new", data={"title": "Prune", "goal": "Learn pruning",
                                  "topic_text": "Pruning keeps storage bounded over time. " * 60},
                    follow_redirects=False)
    assert r.status_code == 303
    deck_id = int(r.headers["location"].split("created=")[1])
    job = None
    for _ in range(60):
        job = db.query_one("SELECT * FROM jobs WHERE deck_id = ?", (deck_id,))
        if job and job["status"] in ("done", "failed"):
            break
        time.sleep(0.2)
    assert job["status"] == "done"

    from app.generation import prune_job_events, JOB_EVENT_RETENTION_DAYS
    # simulate an ancient event + an orphan
    db.execute("INSERT INTO job_events(job_id, level, message, created_at)"
               " VALUES ('ghost-job', 'info', 'orphan', datetime('now', '-999 days'))")
    prune_job_events()
    ghost = db.query_one("SELECT 1 FROM job_events WHERE job_id = 'ghost-job'")
    assert ghost is None
    # real events for live jobs survive
    live = db.query("SELECT * FROM job_events WHERE job_id = ?", (job["id"],))
    assert live

    # regen the text deck: chunks must be rebuilt, questions must regenerate
    r = csrf_post(client, f"/decks/{deck_id}/regen", follow_redirects=False)
    assert r.status_code == 303
    for _ in range(60):
        unfinished = db.query(
            "SELECT * FROM jobs WHERE deck_id = ? AND status IN ('queued','running')", (deck_id,))
        if not unfinished:
            break
        time.sleep(0.2)
    job2 = db.query_one(
        "SELECT * FROM jobs WHERE deck_id = ? ORDER BY rowid DESC LIMIT 1", (deck_id,))
    assert job2["status"] == "done", job2["error"]
    qs = db.query_one("SELECT COUNT(*) AS n FROM questions WHERE deck_id = ?", (deck_id,))
    assert qs["n"] > 0, "regen produced no questions for a text deck"

    # deck delete removes its job events (no orphans)
    csrf_post(client, f"/decks/{deck_id}/delete", follow_redirects=False)
    leftover = db.query_one("SELECT 1 FROM job_events WHERE job_id = ?", (job["id"],))
    assert leftover is None


def test_restart_recovery_and_bounded_concurrency():
    """Interrupted jobs must be marked failed at startup (decks reset to
    draft so regen works), and jobs run on a bounded executor."""
    from app.generation import recover_stuck_jobs, _get_executor, MAX_CONCURRENT_JOBS

    client.post("/register", data={"email": "recover@b.c", "password": "password123",
                                   "display_name": "RV"}, follow_redirects=False)
    r = csrf_post(client, "/new", data={"title": "Stuck", "goal": "Learn recovery",
                                  "topic_text": "Recovery handles interrupted jobs. " * 60},
                    follow_redirects=False)
    assert r.status_code == 303
    deck_id = int(r.headers["location"].split("created=")[1])

    # simulate a crash mid-run: job left 'running'
    db.execute("UPDATE jobs SET status='running' WHERE deck_id = ?", (deck_id,))
    db.execute("UPDATE decks SET status='draft' WHERE id = ?", (deck_id,))

    # the /new job may still be running in a worker thread; wait for it to settle
    import time
    for _ in range(60):
        live = db.query_one("SELECT status FROM jobs WHERE deck_id = ? ORDER BY rowid DESC LIMIT 1",
                            (deck_id,))
        if live["status"] in ("done", "failed"):
            break
        time.sleep(0.2)
    db.execute("UPDATE jobs SET status='running' WHERE deck_id = ?", (deck_id,))
    recover_stuck_jobs()
    job = db.query_one("SELECT * FROM jobs WHERE deck_id = ? ORDER BY rowid DESC LIMIT 1",
                       (deck_id,))
    assert job["status"] == "failed"
    assert "restart" in job["error"].lower()
    deck = db.query_one("SELECT status FROM decks WHERE id = ?", (deck_id,))
    assert deck["status"] == "draft"  # regen is possible again

    # bounded executor: single shared pool with capped workers
    ex = _get_executor()
    assert ex._max_workers == MAX_CONCURRENT_JOBS
    assert _get_executor() is ex  # reused, not per-call

    # after recovery, regen works on the reset deck
    r = csrf_post(client, f"/decks/{deck_id}/regen", follow_redirects=False)
    assert r.status_code == 303
    import time
    for _ in range(60):
        unfinished = db.query(
            "SELECT * FROM jobs WHERE deck_id = ? AND status IN ('queued','running')", (deck_id,))
        if not unfinished:
            break
        time.sleep(0.2)
    job2 = db.query_one("SELECT * FROM jobs WHERE deck_id = ? ORDER BY rowid DESC LIMIT 1",
                        (deck_id,))
    assert job2["status"] == "done", job2["error"]
    qs = db.query_one("SELECT COUNT(*) AS n FROM questions WHERE deck_id = ?", (deck_id,))
    assert qs["n"] > 0


def _register_and_deck(email, title="Sec deck"):
    c = TestClient(app)
    r = c.post("/register", data={"email": email, "password": "password123",
                                   "display_name": email.split("@")[0]}, follow_redirects=False)
    assert r.status_code == 303
    r = csrf_post(c, "/new", data={"title": title, "goal": "goal", "topic_text": "Mitochondria produce ATP. " * 60},
               follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    deck_id = int(r.headers["location"].split("created=")[1])
    import time
    for _ in range(60):
        job = db.query_one("SELECT * FROM jobs WHERE deck_id = ? ORDER BY rowid DESC LIMIT 1", (deck_id,))
        if job and job["status"] in ("done", "failed"):
            break
        time.sleep(0.2)
    assert job["status"] == "done", job["error"]
    q = db.query_one("SELECT * FROM questions WHERE deck_id = ? ORDER BY id LIMIT 1", (deck_id,))
    return c, deck_id, q


def test_cross_user_access_denied():
    """B2: one user must not read, rate, or flag another user's deck/questions."""
    _, deck_id, q = _register_and_deck("owner@example.com")
    other = TestClient(app)
    r = other.post("/register", data={"email": "intruder@example.com", "password": "password123"},
                   follow_redirects=False)
    assert r.status_code == 303

    r = other.get(f"/decks/{deck_id}/questions", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/decks"
    assert "Mitochondria" not in r.text

    r = csrf_post(other, f"/study/{deck_id}/answer",
                   data={"question_id": q["id"], "choice": q["correct_index"]}, follow_redirects=False)
    assert r.status_code == 303
    r = csrf_post(other, f"/study/{deck_id}/rate",
                   data={"question_id": q["id"], "choice": q["correct_index"], "rating": "easy"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert db.query_one("SELECT COUNT(*) c FROM reviews WHERE question_id = ?", (q["id"],))["c"] == 0
    before = dict(db.query_one("SELECT due_at, reps FROM questions WHERE id = ?", (q["id"],)))
    r = csrf_post(other, f"/questions/{q['id']}/flag", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/decks"
    assert db.query_one("SELECT flagged FROM questions WHERE id = ?", (q["id"],))["flagged"] == 0
    after = dict(db.query_one("SELECT due_at, reps FROM questions WHERE id = ?", (q["id"],)))
    assert (after["due_at"], after["reps"]) == (before["due_at"], before["reps"])


def test_flag_without_referer_redirects_safely():
    """B7: flag with no/invalid referer must not produce a relative '?flagged=1' URL."""
    c, deck_id, q = _register_and_deck("flagger@example.com")
    r = csrf_post(c, f"/questions/{q['id']}/flag", headers={"referer": ""}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/")
    assert db.query_one("SELECT flagged FROM questions WHERE id = ?", (q["id"],))["flagged"] == 1


def test_due_at_format_matches_sqlite_now():
    """B1: grade() must emit due_at in SQLite datetime('now') format so that
    SQL 'due_at <= datetime('now')' comparisons actually work."""
    from app.fsrs import grade
    from app import db as _db
    card = {"ef": 2.5, "interval_days": 0, "reps": 0, "lapses": 0}
    new = grade(card, "again")  # due in 1 minute
    assert "T" not in new["due_at"] and "+" not in new["due_at"]
    import sqlite3
    conn = _db.get_conn()
    row = conn.execute("SELECT ? <= datetime('now', '+2 minutes') AS due_soon", (new["due_at"],)).fetchone()
    assert row["due_soon"] == 1, new["due_at"]
    # a card graded 'good' (due in days) is not due now but will be later
    new2 = grade(card, "good")
    row = conn.execute("SELECT ? <= datetime('now') AS due_now, ? <= datetime('now', '+10 days') AS due_later",
                       (new2["due_at"], new2["due_at"])).fetchone()
    assert row["due_now"] == 0 and row["due_later"] == 1


def test_rated_card_becomes_due_again():
    """B1 end-to-end: after rating 'again', the card is due per SQL and /study serves it."""
    c, deck_id, q = _register_and_deck("due@example.com")
    r = csrf_post(c, f"/study/{deck_id}/rate",
               data={"question_id": q["id"], "choice": q["correct_index"], "rating": "good"},
               follow_redirects=False)
    assert r.status_code in (200, 303)
    row = db.query_one("SELECT due_at FROM questions WHERE id = ?", (q["id"],))
    assert "T" not in row["due_at"]
    # force due_at into the past, then check SQL + the /study page agree it is due
    db.execute("UPDATE questions SET due_at = datetime('now', '-1 minute') WHERE id = ?", (q["id"],))
    n = db.query_one("SELECT COUNT(*) c FROM questions WHERE deck_id = ? AND due_at <= datetime('now')",
                     (deck_id,))["c"]
    assert n >= 1
    r = c.get(f"/study/{deck_id}")
    assert r.status_code == 200 and "card-area" in r.text


def test_upload_filename_path_traversal_sanitized():
    """B5: a malicious upload filename must not escape the upload dir."""
    c, _, _ = _register_and_deck("up@example.com")
    evil = b"hello traversal"
    r = csrf_post(c, "/new", data={"title": "T", "goal": "g"},
               files={"file": ("../../../tmp/evil_quipeat.txt", evil, "text/plain")},
               follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    import os
    assert not os.path.exists("/tmp/evil_quipeat.txt")
    assert not any(".." in p for p in os.listdir(os.environ["UPLOAD_DIR"]))


def test_default_secret_key_refused_outside_mock(monkeypatch):
    """E4: the built-in dev SECRET_KEY must not silently boot the app."""
    from app.config import DEFAULT_DEV_SECRET_KEY, validate_secrets
    import app.config as cfg
    import pytest as _pytest
    monkeypatch.setattr(cfg, "settings", cfg.Settings(
        secret_key=DEFAULT_DEV_SECRET_KEY, master_key="x", llm_mock=False))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with _pytest.raises(RuntimeError):
        validate_secrets()


def test_csrf_enforced_on_state_changing_posts():
    """E2: POSTs without a valid session-bound CSRF token are rejected (403),
    except the pre-auth login/register endpoints."""
    c, deck_id, q = _register_and_deck("csrf@example.com")
    # read a page to get the csrf meta value
    page = c.get("/decks")
    import re as _re
    m = _re.search(r'name="csrf-token" content="([0-9a-f]+)"', page.text)
    assert m, "csrf meta tag present"
    tok = m.group(1)
    # missing token -> 403
    r = c.post(f"/decks/{deck_id}/delete", follow_redirects=False)
    assert r.status_code == 403
    # wrong token -> 403
    r = c.post(f"/decks/{deck_id}/delete", data={"csrf": "0" * 64}, follow_redirects=False)
    assert r.status_code == 403
    # correct token -> allowed (303)
    r = csrf_post(c, f"/decks/{deck_id}/delete", data={"csrf": tok}, follow_redirects=False)
    assert r.status_code == 303
    # login/register stay open pre-auth
    c2 = TestClient(app)
    r = c2.post("/login", data={"email": "csrf@example.com", "password": "password123"},
                follow_redirects=False)
    assert r.status_code in (200, 303)


def test_login_rate_limited():
    """E5: after RATE_LIMIT failed attempts from one IP+email, login is throttled."""
    from app.security import RATE_LIMIT, rate_limited, rate_limit_key
    c = TestClient(app)
    c.post("/register", data={"email": "rl@example.com", "password": "password123"}, follow_redirects=False)
    c2 = TestClient(app)
    for _ in range(RATE_LIMIT):
        r = c2.post("/login", data={"email": "rl@example.com", "password": "wrong-password"}, follow_redirects=False)
        assert r.status_code == 200
    r = c2.post("/login", data={"email": "rl@example.com", "password": "wrong-password"}, follow_redirects=False)
    assert r.status_code == 200 and "Too many attempts" in r.text


def test_auth_errors_i18n_pl():
    """E14: PL UI shows translated auth errors."""
    c2 = TestClient(app)
    r = c2.post("/register", data={"email": "pl@example.com", "password": "password123"}, follow_redirects=False)
    assert r.status_code == 303
    db.execute("UPDATE users SET lang = 'pl' WHERE email = 'pl@example.com'")
    r = c2.post("/login", data={"email": "pl@example.com", "password": "nope-nope"}, follow_redirects=False)
    assert r.status_code == 200 and "Nieprawidłowy" in r.text
    c3 = TestClient(app)
    r = c3.post("/register", data={"email": "pl2@example.com", "password": "short"},
                cookies={"quipeat_lang": "pl"}, follow_redirects=False)
    assert r.status_code == 200 and "co najmniej 8" in r.text


def test_500_handler_renders_page():
    """E12: an unhandled error in a page route renders 500.html, not a bare crash."""
    from app import main as m
    @m.app.get("/_boom")
    async def _boom():
        raise RuntimeError("boom")
    c = TestClient(m.app, raise_server_exceptions=False)
    r = c.get("/_boom")
    assert r.status_code == 500 and "Something broke" in r.text
    m.app.routes.remove(next(r for r in m.app.routes if getattr(r, "path", "") == "/_boom"))


def test_normalize_options_never_placeholders_never_stale_correct():
    """F1/F2: dedup tracks correct answer by text; <4 distinct options -> drop;
    placeholders are never produced."""
    from app.generation import _normalize_options, QuestionDraft
    # options A,B,A,C with correct=1 ('B') -> distinct [A,B,C] <4 -> drop
    assert _normalize_options(QuestionDraft(prompt="Pick the correct answer?", options=["A", "B", "A", "C"], correct_index=1, explanation="Because the source says so.")) is None
    # 4 distinct with a duplicate that is NOT the correct one: A,B,B,C correct=3 ('C') -> [A,B,C] <4 -> None
    assert _normalize_options(QuestionDraft(prompt="Pick the correct answer?", options=["A", "B", "B", "C"], correct_index=3, explanation="Because the source says so.")) is None
    # 4 distinct with correct in the middle: A,B,C,D correct=2 ('C') -> unchanged
    q = _normalize_options(QuestionDraft(prompt="Pick the correct answer?", options=["A", "B", "C", "D"], correct_index=2, explanation="Because the source says so."))
    assert q is not None and q.options == ["A", "B", "C", "D"] and q.correct_index == 2
    # case-insensitive duplicate of the correct option: A,B,C,c correct=3 -> 'c' dup of 'C' removed,
    # distinct [A,B,C] <4 -> None (correct text 'c' still maps but too few options)
    assert _normalize_options(QuestionDraft(prompt="Pick the correct answer?", options=["A", "B", "C", "c"], correct_index=3, explanation="Because the source says so.")) is None
    # remap where dedup shifts the correct index: options [X, A, B, A] correct=2 ('B') -> [X,A,B] <4 -> None
    # and [X, Y, A, A] correct=1 ('Y') -> [X,Y,A] <4 -> None; use 4 distinct + 1 dup via direct model bypass:
    from app.generation import QuestionDraft as QD
    raw = QD.model_construct(prompt="Pick the correct answer?", options=["A", "B", "C", "D", "B"], correct_index=2,
                              explanation="e", difficulty="medium")
    q = _normalize_options(raw)
    assert q is not None and q.options == ["A", "B", "C", "D"] and q.correct_index == 2
    # correct option removed as duplicate: A,B,A,C correct=0 ('A') -> [A,B,C] <4 -> None (never clamp)
    assert _normalize_options(QuestionDraft(prompt="Pick the correct answer?", options=["A", "B", "A", "C"], correct_index=0, explanation="Because the source says so.")) is None
    # empty options padded before -> placeholders: must now be None
    assert _normalize_options(QuestionDraft(prompt="Pick the correct answer?", options=["A", "", "B", ""], correct_index=0, explanation="Because the source says so.")) is None
    # placeholder-looking real strings are kept only if 4 distinct
    q = _normalize_options(QuestionDraft(prompt="Pick the correct answer?", options=["X", "Y", "Z", "W"], correct_index=3, explanation="Because the source says so."))
    assert q is not None and q.correct_index == 3 and not any("Distractor" in o for o in q.options)


def test_regen_modes_and_cross_run_dedup():
    """F3/P0-2/P0-3: replace wipes old questions; add-mode dedups against existing
    (normalized prompt and option set), so nothing near-identical is inserted."""
    from app.generation import _is_duplicate, _normalize_text, _option_set_key
    # dedup helpers: paraphrase + diacritics + option set
    seen_p, seen_o = ["What is the powerhouse of the cell?"], set()
    assert _is_duplicate("What is the powerhouse of the cell??", ["a","b","c","d"], seen_p, seen_o)
    assert _is_duplicate("Powerhouse of the cell — what is it?", ["a","b","c","d"], seen_p, seen_o)
    assert _is_duplicate("Co jest elektrownią komórki?", ["a","b","c","d"],
                         ["Co jest elektrownią komórki?"], set())
    key = _option_set_key(["ATP", "Glucose", "Water", "Oxygen"])
    assert _is_duplicate("unrelated prompt", ["oxygen", "water", "glucose", "atp"], [], {key})
    assert not _is_duplicate("Totally different", ["1","2","3","4"], seen_p, seen_o)

    c, deck_id, q = _register_and_deck("regen@example.com")
    n0 = db.query_one("SELECT COUNT(*) c FROM questions WHERE deck_id=?", (deck_id,))["c"]
    # mode=add keeps old questions and job mode is recorded
    r = csrf_post(c, f"/decks/{deck_id}/regen", data={"mode": "add"}, follow_redirects=False)
    assert r.status_code == 303
    import time
    for _ in range(80):
        job = db.query_one("SELECT * FROM jobs WHERE deck_id=? ORDER BY rowid DESC LIMIT 1", (deck_id,))
        if job and job["status"] in ("done", "failed"): break
        time.sleep(0.25)
    assert job["status"] == "done", job["error"]
    assert job["mode"] == "add"
    n1 = db.query_one("SELECT COUNT(*) c FROM questions WHERE deck_id=?", (deck_id,))["c"]
    assert n1 >= n0  # nothing deleted
    # no duplicate normalized prompts in the deck
    prompts = [r["prompt"] for r in db.query("SELECT prompt FROM questions WHERE deck_id=?", (deck_id,))]
    norms = [_normalize_text(p) for p in prompts]
    for i in range(len(norms)):
        for j in range(i + 1, len(norms)):
            assert not (norms[i] and norms[j] and norms[i] == norms[j]), "duplicate prompt survived"
    # mode=replace wipes
    r = csrf_post(c, f"/decks/{deck_id}/regen", data={"mode": "replace"}, follow_redirects=False)
    assert r.status_code == 303
    for _ in range(80):
        job = db.query_one("SELECT * FROM jobs WHERE deck_id=? ORDER BY rowid DESC LIMIT 1", (deck_id,))
        if job and job["status"] in ("done", "failed"): break
        time.sleep(0.25)
    assert job["status"] == "done" and job["mode"] == "replace"
    n2 = db.query_one("SELECT COUNT(*) c FROM questions WHERE deck_id=?", (deck_id,))["c"]
    assert n2 > 0 and n2 <= n1  # regenerated fresh, old ones gone


def test_v2_prompts_contain_wording_and_clarity_rules():
    """P0-4/P0-5: generate_v2 has wording rules, verify_v2 has clarity checks."""
    from app.generation import _system_prompt, PROMPT_VERSION
    assert PROMPT_VERSION == "v2"
    gen = _system_prompt("generate")
    for needle in ("40 words", "12 words", "double negative", "all of the above",
                   "meta references", "parallel", "plausible confusions", "mandatory"):
        assert needle.lower() in gen.lower(), f"generate_v2 missing: {needle}"
    ver = _system_prompt("verify")
    for needle in ("length", "convoluted", "double negative", "meta references",
                   "parallelism", "exactly one defensible", "notes"):
        assert needle.lower() in ver.lower(), f"verify_v2 missing: {needle}"


def test_explanations_mandatory_and_notes_stored():
    """F9/F11: no question enters the DB with an empty explanation; verify notes
    are stored; job counts generated/kept/dropped."""
    c, deck_id, q = _register_and_deck("expl@example.com")
    rows = db.query("SELECT explanation, verify_notes FROM questions WHERE deck_id=?", (deck_id,))
    assert rows and all(r["explanation"].strip() for r in rows), "empty explanation in DB"
    job = db.query_one("SELECT generated, kept, dropped, mode FROM jobs WHERE deck_id=? ORDER BY rowid DESC LIMIT 1", (deck_id,))
    assert job["kept"] >= 1 and job["generated"] >= job["kept"]
    assert job["generated"] == job["kept"] + job["dropped"]
    # pydantic rejects empty explanation at parse time
    from app.generation import QuestionDraft
    import pytest as _pytest
    with _pytest.raises(Exception):
        QuestionDraft(prompt="A valid length prompt?", options=["a","b","c","d"],
                      correct_index=0, explanation="")


def test_target_size_drives_count_and_shortfall_reported():
    """P1-7: target is stored on the deck, drives per-chunk density, and a small
    source either hits the target via extra passes or reports a shortfall reason."""
    from app.config import MAX_PASSES_PER_CHUNK, MAX_QUESTIONS_PER_CHUNK
    c = TestClient(app)
    c.post("/register", data={"email": "tgt@example.com", "password": "password123"}, follow_redirects=False)
    # tiny source, target 200 -> cannot be reached; shortfall message must say why
    r = csrf_post(c, "/new", data={"title": "Tiny", "goal": "g", "topic_text": "One short fact here. " * 5,
                                   "target": "200"}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    deck_id = int(r.headers["location"].split("created=")[1])
    deck = db.query_one("SELECT target_questions FROM decks WHERE id=?", (deck_id,))
    assert deck["target_questions"] == 200
    import time
    for _ in range(120):
        job = db.query_one("SELECT * FROM jobs WHERE deck_id=? ORDER BY rowid DESC LIMIT 1", (deck_id,))
        if job and job["status"] in ("done", "failed"): break
        time.sleep(0.25)
    assert job["status"] == "done", job["error"]
    n = db.query_one("SELECT COUNT(*) c FROM questions WHERE deck_id=?", (deck_id,))["c"]
    max_possible = 1 * MAX_QUESTIONS_PER_CHUNK * MAX_PASSES_PER_CHUNK
    assert n <= max_possible
    if n < 200:
        assert "Shortfall" in job["message"] or "target" in job["message"].lower(), job["message"]
    # target 20 on a normal source: count should be <= 20 and > 0
    r = csrf_post(c, "/new", data={"title": "Small", "goal": "g", "topic_text": "Mitochondria produce ATP. " * 60,
                                   "target": "20"}, follow_redirects=False)
    deck2 = int(r.headers["location"].split("created=")[1])
    for _ in range(120):
        job = db.query_one("SELECT * FROM jobs WHERE deck_id=? ORDER BY rowid DESC LIMIT 1", (deck2,))
        if job and job["status"] in ("done", "failed"): break
        time.sleep(0.25)
    n2 = db.query_one("SELECT COUNT(*) c FROM questions WHERE deck_id=?", (deck2,))["c"]
    assert 0 < n2 <= 20, n2
    # invalid target falls back to default
    r = csrf_post(c, "/new", data={"title": "Bad", "goal": "g", "topic_text": "x " * 100, "target": "999"},
                  follow_redirects=False)
    deck3 = int(r.headers["location"].split("created=")[1])
    assert db.query_one("SELECT target_questions FROM decks WHERE id=?", (deck3,))["target_questions"] == 50


def test_topic_mode_research_doc_then_questions():
    """P1-8/F4: bare topic -> kind='topic' source with generated research text,
    normal pipeline runs on it, count driven by target, deck page discloses."""
    c = TestClient(app)
    c.post("/register", data={"email": "topic@example.com", "password": "password123"}, follow_redirects=False)
    r = csrf_post(c, "/new", data={"title": "Quantum computing", "goal": "Understand qubits",
                                   "topic_text": "Quantum computing", "source_mode": "topic",
                                   "target": "20"}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    deck_id = int(r.headers["location"].split("created=")[1])
    src = db.query_one("SELECT kind FROM sources s JOIN decks d ON d.source_id=s.id WHERE d.id=?", (deck_id,))
    assert src["kind"] == "topic"
    import time
    for _ in range(120):
        job = db.query_one("SELECT * FROM jobs WHERE deck_id=? ORDER BY rowid DESC LIMIT 1", (deck_id,))
        if job and job["status"] in ("done", "failed"): break
        time.sleep(0.25)
    assert job["status"] == "done", job["error"]
    n = db.query_one("SELECT COUNT(*) c FROM questions WHERE deck_id=?", (deck_id,))["c"]
    assert 0 < n <= 20, n
    src = db.query_one("SELECT raw_text FROM sources s JOIN decks d ON d.source_id=s.id WHERE d.id=?", (deck_id,))
    assert "# Overview of" in src["raw_text"], "research doc not stored"
    # deck page discloses generated-research grounding
    page = c.get(f"/decks/{deck_id}")
    assert "grounded in generated research" in page.text


def test_chunking_v2_never_cuts_sentences_and_drops_boilerplate():
    """F6/P1-9: paragraph-structured input chunks at sentence boundaries; headings
    kept with their paragraph; boilerplate chunks dropped; overlap preserved."""
    from app.ingest import chunk_text_v2
    paras = []
    for p in range(30):
        paras.append(f"# Section {p}\n\n" + " ".join(f"Sentence {p}.{s} has content." for s in range(12)))
    text = "\n\n".join(paras)
    chunks = chunk_text_v2(text, chunk_tokens=60, overlap_tokens=15)
    assert len(chunks) > 3
    for c in chunks:
        assert len(c.split()) <= 60 or c.endswith((".", "!", "?")), "oversized chunk not sentence-terminated"
        # every chunk must end at a sentence boundary (or be a single long sentence)
        assert c.rstrip().endswith((".", "!", "?", "…")) or len(c.split()) > 60
    # unpunctuated run: chunk boundaries must not split mid-"sentence" (no terminators exist,
    # so chunks are pure word windows — but with punctuation present they align)
    punct = "The cell produces ATP. " * 100
    ch = chunk_text_v2(punct, chunk_tokens=20, overlap_tokens=5)
    assert all(c.rstrip().endswith(".") for c in ch), [c[-20:] for c in ch]
    # boilerplate dropped
    boiler = chunk_text_v2("References\n\n" + "Real content sentence here. " * 50 + "\n\n1.\n\n2.", chunk_tokens=30)
    assert all(len(c.split()) >= 8 for c in boiler)
    assert not any(c.strip() in ("References", "1.", "2.") for c in boiler)
    # overlap: consecutive chunks share trailing words
    big = " ".join(f"Word{i} sentence ends here." for i in range(200))
    ch2 = chunk_text_v2(big, chunk_tokens=40, overlap_tokens=10)
    shared = set(ch2[0].split()) & set(ch2[1].split())
    assert len(shared) > 0, "no overlap between consecutive chunks"


def test_job_ui_shows_stage_and_counts():
    """P1-10: job row has stage + generated/kept/dropped and the status fragment
    renders them."""
    c, deck_id, q = _register_and_deck("jobui@example.com")
    job = db.query_one("SELECT * FROM jobs WHERE deck_id=? ORDER BY rowid DESC LIMIT 1", (deck_id,))
    assert job["stage"] == "done"
    assert job["generated"] == job["kept"] + job["dropped"]
    frag = c.get(f"/decks/{deck_id}/job-status")
    assert frag.status_code == 200
    assert "kept" in frag.text and "dropped" in frag.text


def test_flagged_excluded_from_study_and_listed_on_deck():
    """F10/P2-13: a flagged question is never served by /study and shows on the
    deck page with unflag/delete actions."""
    c, deck_id, q = _register_and_deck("flagstudy@example.com")
    # force the question due and flag it
    db.execute("UPDATE questions SET due_at = datetime('now','-1 minute')", ())
    for row in db.query("SELECT id FROM questions WHERE deck_id=?", (deck_id,)):
        csrf_post(c, f"/questions/{row['id']}/flag")
    r = csrf_post(c, f"/questions/{q['id']}/flag")
    assert r.status_code in (200, 303)
    assert db.query_one("SELECT flagged FROM questions WHERE id=?", (q["id"],))["flagged"] == 1
    r = c.get(f"/study/{deck_id}")
    assert r.status_code == 200 and q["prompt"][:30] not in r.text, "flagged question served in study"
    # due_count excludes flagged
    n = db.query_one("SELECT COUNT(*) c FROM questions WHERE deck_id=? AND due_at <= datetime('now') AND flagged=0", (deck_id,))["c"]
    assert n == 0
    # deck page lists it
    page = c.get(f"/decks/{deck_id}")
    assert "Flagged questions" in page.text
    # unflag restores it to the queue
    for row in db.query("SELECT id FROM questions WHERE deck_id=?", (deck_id,)):
        csrf_post(c, f"/questions/{row['id']}/unflag")
    r = c.get(f"/study/{deck_id}")
    assert q["prompt"][:30] in r.text


def test_question_edit_delete_regen_one():
    """P2-12: edit sets edited=1 and validates 4 distinct options; delete removes;
    regen-one replaces the row via mock pipeline."""
    c, deck_id, q = _register_and_deck("editq@example.com")
    # edit
    r = csrf_post(c, f"/questions/{q['id']}/edit", data={
        "prompt": "Edited prompt about mitochondria and ATP production?",
        "opt0": "ATP", "opt1": "DNA", "opt2": "RNA", "opt3": "Lipid",
        "correct": "0", "explanation": "Mitochondria produce ATP via oxidative phosphorylation."})
    assert r.status_code == 303
    row = db.query_one("SELECT * FROM questions WHERE id=?", (q["id"],))
    assert row["edited"] == 1 and row["prompt"].startswith("Edited prompt")
    import json as _json
    assert _json.loads(row["options_json"]) == ["ATP", "DNA", "RNA", "Lipid"] and row["correct_index"] == 0
    # invalid edit (duplicate options) rejected
    r = csrf_post(c, f"/questions/{q['id']}/edit", data={
        "prompt": "Another edited prompt here?", "opt0": "A", "opt1": "A", "opt2": "B", "opt3": "C",
        "correct": "0", "explanation": "x"})
    assert r.status_code == 303 and "error=invalid" in r.headers["location"]
    # regen-one
    r = csrf_post(c, f"/questions/{q['id']}/regen")
    assert r.status_code == 303 and "regen=1" in r.headers["location"]
    row2 = db.query_one("SELECT * FROM questions WHERE id=?", (q["id"],))
    assert row2["prompt"] != "Edited prompt about mitochondria and ATP production?" or row2["edited"] == 1
    # delete
    r = csrf_post(c, f"/questions/{q['id']}/delete")
    assert r.status_code == 303
    assert db.query_one("SELECT 1 FROM questions WHERE id=?", (q["id"],)) is None


def test_edited_questions_survive_add_more_regen():
    """Acceptance: edited questions are untouched by regen mode=add."""
    c, deck_id, q = _register_and_deck("editedregen@example.com")
    csrf_post(c, f"/questions/{q['id']}/edit", data={
        "prompt": "Hand-edited question that must survive regen?",
        "opt0": "One", "opt1": "Two", "opt2": "Three", "opt3": "Four",
        "correct": "2", "explanation": "Because the user said so explicitly."})
    r = csrf_post(c, f"/decks/{deck_id}/regen", data={"mode": "add"}, follow_redirects=False)
    assert r.status_code == 303
    import time
    for _ in range(80):
        job = db.query_one("SELECT * FROM jobs WHERE deck_id=? ORDER BY rowid DESC LIMIT 1", (deck_id,))
        if job and job["status"] in ("done", "failed"): break
        time.sleep(0.25)
    assert job["status"] == "done", job["error"]
    row = db.query_one("SELECT * FROM questions WHERE id=?", (q["id"],))
    assert row is not None and row["prompt"].startswith("Hand-edited") and row["edited"] == 1
