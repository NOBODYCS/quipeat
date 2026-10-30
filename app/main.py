import hmac
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, Response, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, gamify
from .fsrs import grade, xp_for
from .config import DEFAULT_LEVEL, DEFAULT_TARGET, LEVELS, TARGET_CHOICES, settings
from .i18n import LANGS, STRINGS, translate
from .generation import start_generation
from .ingest import chunk_text, chunk_text_v2, extract_text_from_file, extract_text_from_url, IngestError
from .llm.providers import DEFAULT_MODELS
from .security import (create_session, csrf_token, decrypt_secret, delete_session, encrypt_secret,
                       get_session_user, hash_password, mask_key, rate_limit_key, rate_limited,
                       verify_password)

BASE_DIR = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("quipeat.main")

@asynccontextmanager
async def _lifespan(app):
    _startup()
    yield


app = FastAPI(title="Quipeat", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")

# Anonymous POSTs that must stay open pre-auth (login/register/passkey ceremony).
_CSRF_EXEMPT_POSTS = {"/login", "/register", "/passkeys/login", "/passkeys/login-options"}


@app.middleware("http")
async def _csrf_guard(request: Request, call_next):
    """Reject state-changing requests whose CSRF token (form field `csrf` or
    `X-CSRF-Token` header) does not match the session-bound HMAC."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if request.url.path not in _CSRF_EXEMPT_POSTS:
            token = request.cookies.get("quipeat_session")
            if token:
                expected = csrf_token(token)
                supplied = request.headers.get("x-csrf-token", "")
                if not supplied and "form" in request.headers.get("content-type", ""):
                    # Parse the body ourselves and stash it in request._form;
                    # FastAPI's route handling reuses request._form when set.
                    body = await request.body()
                    from starlette.formparsers import FormParser, MultiPartParser, parse_options_header
                    from urllib.parse import parse_qsl
                    ct, _ = parse_options_header(request.headers.get("Content-Type"))
                    if ct == b"multipart/form-data":
                        async def _mp():
                            yield body
                        request._form = await MultiPartParser(request.headers, _mp()).parse()
                    elif ct == b"application/x-www-form-urlencoded":
                        from starlette.datastructures import FormData
                        request._form = FormData(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
                    request._body = body
                    supplied = (request._form or {}).get("csrf", "")
                if not hmac.compare_digest(supplied, expected):
                    log.warning("CSRF rejected: %s %s", request.method, request.url.path)
                    return Response("CSRF token missing or invalid", status_code=403)
    return await call_next(request)


def _startup():
    from .config import validate_secrets
    validate_secrets()
    db.init_db()
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    from .security import prune_expired_sessions
    prune_expired_sessions()
    from . import diagnostics
    from .generation import prune_job_events, recover_stuck_jobs
    prune_job_events()
    recover_stuck_jobs()
    results = diagnostics.run_all_checks()
    degraded = [f"{k}: {v['detail']}" for k, v in results.items() if v["status"] != "ok"]
    if degraded:
        log.warning("startup diagnostics: degraded components -> %s", "; ".join(degraded))
    else:
        log.info("startup diagnostics: all components ok")


@app.get("/health")
async def health():
    from . import diagnostics
    results = diagnostics.run_all_checks()
    overall = "ok" if all(v["status"] == "ok" for v in results.values()) else "degraded"
    return {"status": overall, "components": results}


def lang_of_user(user) -> str:
    if user and "lang" in user.keys() and user["lang"] in LANGS:
        return user["lang"]
    return "en"


def current_user(request: Request):
    return get_session_user(request.cookies.get("quipeat_session"))


def lang_of(request: Request, user=None) -> str:
    """Effective UI language: user preference, else the guest lang cookie."""
    if user is None:
        user = current_user(request)
    if user and "lang" in user.keys() and user["lang"] in LANGS:
        return user["lang"]
    cookie = request.cookies.get("quipeat_lang")
    return cookie if cookie in LANGS else "en"


def require_user(request: Request):
    user = current_user(request)
    if not user:
        raise _Redirect("/login")
    return user


def owned_deck(user, deck_id: int):
    """Deck row only if it belongs to this user; None otherwise."""
    return db.query_one("SELECT * FROM decks WHERE id = ? AND user_id = ?", (deck_id, user["id"]))


def owned_question(user, deck_id: int, question_id: int):
    """Question row only if its deck belongs to this user; None otherwise."""
    return db.query_one(
        "SELECT q.* FROM questions q JOIN decks d ON d.id = q.deck_id"
        " WHERE q.id = ? AND q.deck_id = ? AND d.user_id = ?",
        (question_id, deck_id, user["id"]))


class _Redirect(Exception):
    def __init__(self, location: str):
        self.location = location


@app.exception_handler(_Redirect)
async def _redirect_handler(request: Request, exc: _Redirect):
    return RedirectResponse(exc.location, status_code=303)


def page(request: Request, template: str, **ctx) -> HTMLResponse:
    user = current_user(request)
    lang = lang_of(request, user)
    if lang not in LANGS:
        lang = "en"
    ctx.setdefault("request", request)
    ctx.setdefault("user", user)
    ctx.setdefault("passkeys_enabled", settings.passkeys_enabled)
    ctx.setdefault("lang", lang)
    ctx.setdefault("langs", LANGS)
    ctx.setdefault("levels", LEVELS)
    ctx.setdefault("default_level", DEFAULT_LEVEL)
    ctx.setdefault("t", lambda k, **fmt: translate(lang, k, **fmt))
    sess_token = request.cookies.get("quipeat_session")
    ctx.setdefault("csrf", csrf_token(sess_token) if sess_token else "")
    if user:
        from .fsrs import level_progress
        lvl, into, need = level_progress(user["xp_total"])
        # one query for review stats + badge window + badge total
        stats = db.query_one(
            "SELECT (SELECT COUNT(*) FROM reviews WHERE user_id = :u) AS rev_c,"
            "       (SELECT COALESCE(SUM(correct),0) FROM reviews WHERE user_id = :u) AS rev_ok,"
            "       (SELECT COUNT(*) FROM user_badges WHERE user_id = :u) AS badge_total",
            {"u": user["id"]})
        badges = db.query(
            "SELECT badge FROM user_badges WHERE user_id = ? ORDER BY awarded_at DESC LIMIT 5",
            (user["id"],))
        rev_c, rev_ok = stats["rev_c"], stats["rev_ok"]
        ctx.setdefault("sidebar", {
            "level": lvl, "xp_into": into, "xp_need": need,
            "pct": min(100, round(100 * into / need)) if need else 100,
            "streak": user["streak_days"],
            "reviews": rev_c,
            "accuracy": round(100 * rev_ok / rev_c, 1) if rev_c else None,
            "badges": badges, "badge_total": stats["badge_total"],
        })
    return templates.TemplateResponse(request, template, ctx)


# ---------- auth ----------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if current_user(request):
        return RedirectResponse("/decks", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/register", response_class=HTMLResponse)
async def register_form(request: Request):
    return page(request, "register.html", error=None)


@app.post("/register")
async def register(request: Request, email: str = Form(...), password: str = Form(...),
                   display_name: str = Form("")):
    email = email.strip().lower()
    if rate_limited(rate_limit_key(request, email)):
        return page(request, "register.html", error=translate(lang_of(request), "rate_limited"))
    if len(password) < 8:
        return page(request, "register.html", error=translate(lang_of(request), "pw_too_short"))
    if db.query_one("SELECT 1 FROM users WHERE email = ?", (email,)):
        return page(request, "register.html", error=translate(lang_of(request), "email_taken"))
    cur = db.execute("INSERT INTO users(email, password_hash, display_name) VALUES (?, ?, ?)",
                     (email, hash_password(password), display_name.strip()))
    uid = cur.lastrowid
    ident = settings.admin_ident.strip()
    if ident and (email == ident.lower() or display_name.strip().lower() == ident.lower()):
        db.execute("UPDATE users SET role = 'member' WHERE role = 'admin' AND id != ?", (uid,))
        db.execute("UPDATE users SET role = 'admin' WHERE id = ?", (uid,))
    elif not db.query_one("SELECT 1 FROM users WHERE role = 'admin'"):
        db.execute("UPDATE users SET role = 'admin' WHERE id = ?", (uid,))
    db.execute("INSERT INTO user_settings(user_id) VALUES (?)", (uid,))
    token = create_session(cur.lastrowid)
    resp = RedirectResponse("/onboarding", status_code=303)
    resp.set_cookie("quipeat_session", token, httponly=True, samesite="lax", max_age=30 * 86400)
    return resp


@app.exception_handler(404)
async def _not_found(request: Request, exc):
    if request.url.path.startswith("/static/") or request.url.path.startswith("/api/"):
        return HTMLResponse("Not found", status_code=404)
    return page(request, "404.html")


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    if request.url.path.startswith("/static/") or request.url.path.startswith("/api/"):
        return Response("Internal server error", status_code=500)
    try:
        return templates.TemplateResponse(request, "500.html", {
            "request": request, "user": current_user(request),
            "passkeys_enabled": settings.passkeys_enabled,
            "lang": lang_of(request), "langs": LANGS,
            "t": lambda k: translate(lang_of(request), k),
            "csrf": csrf_token(request.cookies.get("quipeat_session") or ""),
        }, status_code=500)
    except Exception:
        return HTMLResponse("Internal server error", status_code=500)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str | None = None):
    return page(request, "login.html", error=error)


@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    if rate_limited(rate_limit_key(request, email)):
        return page(request, "login.html", error=translate(lang_of(request), "rate_limited"))
    user = db.query_one("SELECT * FROM users WHERE email = ?", (email,))
    if not user or not verify_password(password, user["password_hash"]):
        return page(request, "login.html", error=translate(lang_of(request), "invalid_creds"))
    token = create_session(user["id"])
    resp = RedirectResponse("/onboarding" if not user["onboarded_at"] else "/decks", status_code=303)
    resp.set_cookie("quipeat_session", token, httponly=True, samesite="lax", max_age=30 * 86400)
    return resp


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding(request: Request, step: int = 1, replay: int = 0):
    user = require_user(request)
    if user["onboarded_at"] and not replay:
        return RedirectResponse("/decks", status_code=303)
    step = max(1, min(7, step))
    return page(request, "onboarding.html", step=step, total_steps=7, replay=bool(replay))


@app.post("/onboarding/finish")
async def onboarding_finish(request: Request, replay: int = Form(0)):
    user = require_user(request)
    db.execute("UPDATE users SET onboarded_at = datetime('now') WHERE id = ?", (user["id"],))
    if replay:
        return RedirectResponse("/decks", status_code=303)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/lang")
async def set_lang(request: Request, lang: str = Form(...)):
    """Switch UI language. Works for guests (cookie) and signed-in users (profile)."""
    if lang not in LANGS:
        lang = "en"
    user = current_user(request)
    resp = RedirectResponse(request.headers.get("referer") or "/decks", status_code=303)
    resp.set_cookie("quipeat_lang", lang, max_age=60 * 60 * 24 * 365, samesite="lax")
    if user:
        db.execute("UPDATE users SET lang = ? WHERE id = ?", (lang, user["id"]))
        target = resp.headers["location"]
        if "msg=" not in target:
            from urllib.parse import quote
            target += ("&" if "?" in target else "?") + "msg=" + quote(translate(lang, "language_saved"))
            resp = RedirectResponse(target, status_code=303)
            resp.set_cookie("quipeat_lang", lang, max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get("quipeat_session")
    if token:
        delete_session(token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("quipeat_session")
    return resp


# ---------- decks & sources ----------

def _expected_questions(deck_id: int) -> int:
    """Honest estimate for the job bar: the deck's target, capped by what the
    source's chunks can actually yield (target for URL decks still crawling)."""
    deck = db.query_one("SELECT source_id, target_questions FROM decks WHERE id = ?", (deck_id,))
    if not deck:
        return 0
    target = deck["target_questions"] if deck["target_questions"] in TARGET_CHOICES else DEFAULT_TARGET
    n_chunks = db.query_one("SELECT COUNT(*) AS n FROM chunks WHERE source_id = ?",
                            (deck["source_id"],))["n"]
    if n_chunks:
        from .config import MAX_PASSES_PER_CHUNK, MAX_QUESTIONS_PER_CHUNK
        return min(target, n_chunks * MAX_QUESTIONS_PER_CHUNK * MAX_PASSES_PER_CHUNK)
    return target

@app.get("/decks", response_class=HTMLResponse)
async def decks(request: Request, created: int | None = None):
    user = require_user(request)
    rows = db.query(
        "SELECT d.*, s.kind AS source_kind, (SELECT COUNT(*) FROM questions q WHERE q.deck_id = d.id) AS q_count,"
        " (SELECT COUNT(*) FROM questions q WHERE q.deck_id = d.id AND q.due_at <= datetime('now') AND q.flagged = 0) AS due_count"
        " FROM decks d JOIN sources s ON s.id = d.source_id WHERE d.user_id = ? ORDER BY d.created_at DESC",
        (user["id"],))
    from .mastery import deck_mastery, deck_level
    enriched = []
    for d in rows:
        m = deck_mastery(user["id"], d["id"])
        d = dict(d)
        d["mastery_pct"] = m["pct"]
        d["deck_level"] = deck_level(m["pct"])
        enriched.append(d)
    return page(request, "decks.html", decks=enriched, created=created)


@app.get("/new", response_class=HTMLResponse)
async def new_source_form(request: Request):
    require_user(request)
    return page(request, "new_source.html", error=None, target_choices=TARGET_CHOICES, default_target=DEFAULT_TARGET)


@app.post("/new")
async def new_source(request: Request, title: str = Form(...), goal: str = Form(""),
                     url: str = Form(""), topic_text: str = Form(""),
                     source_mode: str = Form("text"),
                     lang: str = Form("en"), level: str = Form(DEFAULT_LEVEL),
                     target: int = Form(DEFAULT_TARGET),
                     file: UploadFile | None = File(None)):
    user = require_user(request)
    title = title.strip()
    goal = goal.strip()
    url = url.strip()
    topic_text = topic_text.strip()
    if lang not in LANGS:
        lang = user["lang"] if user["lang"] in LANGS else "en"
    if level not in LEVELS:
        level = DEFAULT_LEVEL
    if target not in TARGET_CHOICES:
        target = DEFAULT_TARGET
    try:
        if file and file.filename:
            data = await file.read()
            if len(data) > settings.max_upload_mb * 1024 * 1024:
                raise IngestError("__err_file_too_big")
            upload_dir = Path(settings.upload_dir)
            upload_dir.mkdir(parents=True, exist_ok=True)
            safe_name = Path(file.filename).name  # strip any path components
            tmp = upload_dir / f"{secrets.token_hex(8)}_{safe_name}"
            tmp.write_bytes(data)
            try:
                raw = extract_text_from_file(tmp, safe_name)
            finally:
                tmp.unlink(missing_ok=True)
            kind, ref = "file", safe_name
        elif url:
            raw = ""
            kind, ref = "url", url
        elif topic_text and source_mode == "topic":
            # Topic mode: the text is a TOPIC NAME, not source material. The
            # background job writes a research document on it first (kind='topic').
            raw = ""
            kind, ref = "topic", topic_text
        elif topic_text:
            raw = topic_text
            kind, ref = "text", None
        else:
            raise IngestError("__err_empty_source")
    except IngestError as e:
        key = str(e)
        if key.startswith("__"):
            key = key[2:]
        else:
            key = "err_generic"
        return page(request, "new_source.html", error=translate(lang_of(request, user), key))
    if kind not in ("url", "topic") and not raw.strip():
        return page(request, "new_source.html", error=translate(lang_of(request, user), "err_no_text"))
    if not goal:
        return page(request, "new_source.html", error=translate(lang_of(request, user), "err_no_goal"),
                    title=title, goal=goal, url=url, topic_text=topic_text, level=level,
                    target_choices=TARGET_CHOICES, default_target=DEFAULT_TARGET)
    if not title:
        title = (ref or "Untitled")[:80]
    cur = db.execute("INSERT INTO sources(user_id, kind, title, original_ref, raw_text) VALUES (?, ?, ?, ?, ?)",
                     (user["id"], kind, title, ref, raw))
    source_id = cur.lastrowid
    if kind != "topic":  # topic sources are chunked by the job after the research doc is written
        for idx, piece in enumerate(chunk_text_v2(raw)):
            db.execute("INSERT INTO chunks(source_id, idx, text) VALUES (?, ?, ?)", (source_id, idx, piece))
    cur = db.execute("INSERT INTO decks(user_id, source_id, title, goal, lang, level, target_questions)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (user["id"], source_id, title, goal, lang, level, target))
    deck_id = cur.lastrowid
    job_id = start_generation(user["id"], deck_id)
    return RedirectResponse(f"/decks?created={deck_id}", status_code=303)


@app.get("/decks/{deck_id}", response_class=HTMLResponse)
async def deck_detail(request: Request, deck_id: int, job: str | None = None):
    user = require_user(request)
    deck = db.query_one(
        "SELECT d.*, s.kind AS source_kind, s.original_ref, s.title AS source_title,"
        " (SELECT COUNT(*) FROM questions q WHERE q.deck_id = d.id) AS q_count,"
        " (SELECT COUNT(*) FROM questions q WHERE q.deck_id = d.id AND q.due_at <= datetime('now') AND q.flagged = 0) AS due_count"
        " FROM decks d JOIN sources s ON s.id = d.source_id WHERE d.id = ? AND d.user_id = ?",
        (deck_id, user["id"]))
    if not deck:
        return RedirectResponse("/decks", status_code=303)
    job_row = db.query_one("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job, user["id"])) if job else None
    if not job_row and deck["status"] in ("ready", "failed"):
        job_row = db.query_one(
            "SELECT * FROM jobs WHERE deck_id = ? AND user_id = ? ORDER BY created_at DESC LIMIT 1",
            (deck_id, user["id"]))
    flagged = db.query(
        "SELECT q.* FROM questions q WHERE q.deck_id = ? AND q.flagged = 1 ORDER BY q.id",
        (deck_id,))
    flagged_items = [{**dict(f), "options": json.loads(f["options_json"])} for f in flagged]
    return page(request, "deck_detail.html", deck=deck, job=job_row, flagged=flagged_items,
                expected_q=_expected_questions(deck_id))


@app.get("/decks/{deck_id}/job-status", response_class=HTMLResponse)
async def job_status(request: Request, deck_id: int):
    user = require_user(request)
    job = db.query_one(
        "SELECT * FROM jobs WHERE deck_id = ? AND user_id = ? ORDER BY created_at DESC LIMIT 1",
        (deck_id, user["id"]))
    if not job:
        return HTMLResponse("")
    return page(request, "_job_status.html", job=job, deck_id=deck_id,
                expected_q=_expected_questions(deck_id))


@app.get("/decks/{deck_id}/questions", response_class=HTMLResponse)
async def deck_questions(request: Request, deck_id: int):
    user = require_user(request)
    deck = owned_deck(user, deck_id)
    if not deck:
        return RedirectResponse("/decks", status_code=303)
    qs = db.query("SELECT * FROM questions WHERE deck_id = ? ORDER BY id", (deck_id,))
    items = [{**dict(q), "options": json.loads(q["options_json"])} for q in qs]
    return page(request, "questions.html", questions=items, deck=deck)


@app.get("/decks/{deck_id}/inspect", response_class=HTMLResponse)
async def deck_inspect(request: Request, deck_id: int):
    """Read-only study view of every question in the deck. Answers hidden by
    default; revealed per-question via JS toggle. The true answer is only
    revealable when the user has reviewed the question before."""
    user = require_user(request)
    deck = owned_deck(user, deck_id)
    if not deck:
        return RedirectResponse("/decks", status_code=303)
    qs = db.query(
        "SELECT q.*, EXISTS(SELECT 1 FROM reviews r WHERE r.question_id = q.id AND r.user_id = ?)"
        " AS answered FROM questions q WHERE q.deck_id = ? ORDER BY q.id",
        (user["id"], deck_id))
    items = [{**dict(q), "options": json.loads(q["options_json"])} for q in qs]
    return page(request, "inspect.html", questions=items, deck=deck)


# ---------- topic pack library ----------

@app.get("/library", response_class=HTMLResponse)
async def library_page(request: Request):
    require_user(request)
    from .packs import list_shipped_packs
    return page(request, "library.html", packs=list_shipped_packs())


@app.post("/library/clone")
async def library_clone(request: Request, pack_file: str = Form(...)):
    user = require_user(request)
    from .packs import load_pack_file, import_pack_into_deck
    try:
        pack = load_pack_file(pack_file)
    except ValueError:
        return RedirectResponse("/library?error=invalid", status_code=303)
    deck_id = import_pack_into_deck(user["id"], pack)
    return RedirectResponse(f"/decks?created={deck_id}", status_code=303)


@app.post("/library/import")
async def library_import(request: Request, file: UploadFile = File(...)):
    user = require_user(request)
    import json as _json
    from .packs import validate_pack, import_pack_into_deck
    try:
        data = _json.loads((await file.read()).decode("utf-8"))
        pack = validate_pack(data)
    except Exception:
        return RedirectResponse("/library?error=invalid", status_code=303)
    deck_id = import_pack_into_deck(user["id"], pack)
    return RedirectResponse(f"/decks?created={deck_id}", status_code=303)


@app.get("/decks/{deck_id}/export.pack.json")
async def deck_export(request: Request, deck_id: int):
    user = require_user(request)
    deck = owned_deck(user, deck_id)
    if not deck:
        return RedirectResponse("/decks", status_code=303)
    from .packs import export_deck_pack
    qs = db.query("SELECT * FROM questions WHERE deck_id = ? ORDER BY id", (deck_id,))
    pack = export_deck_pack(deck, qs)
    return Response(
        json.dumps(pack, ensure_ascii=False, indent=1),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{deck["title"][:40].replace(" ", "_")}.pack.json"'})


# ---------- VS duels ----------

@app.get("/duels", response_class=HTMLResponse)
async def duels_page(request: Request):
    user = require_user(request)
    from . import duels as du
    incoming = db.query(
        "SELECT d.*, u.display_name AS challenger_name, dk.title AS deck_title"
        " FROM duels d JOIN users u ON u.id = d.challenger_id JOIN decks dk ON dk.id = d.deck_id"
        " WHERE d.opponent_id = ? AND d.status = 'pending' ORDER BY d.created_at DESC", (user["id"],))
    active = db.query(
        "SELECT d.*, u.display_name AS challenger_name, dk.title AS deck_title"
        " FROM duels d JOIN users u ON u.id = d.challenger_id JOIN decks dk ON dk.id = d.deck_id"
        " WHERE d.status = 'active' AND (d.challenger_id = ? OR d.opponent_id = ?) ORDER BY d.started_at DESC",
        (user["id"], user["id"]))
    history = db.query(
        "SELECT d.*, uc.display_name AS challenger_name, uo.display_name AS opponent_name, dk.title AS deck_title"
        " FROM duels d JOIN users uc ON uc.id = d.challenger_id JOIN users uo ON uo.id = d.opponent_id"
        " JOIN decks dk ON dk.id = d.deck_id"
        " WHERE d.status = 'finished' AND (d.challenger_id = ? OR d.opponent_id = ?) ORDER BY d.finished_at DESC LIMIT 20",
        (user["id"], user["id"]))
    my_decks = db.query("SELECT id, title FROM decks WHERE user_id = ? AND status = 'ready' ORDER BY title", (user["id"],))
    others = db.query("SELECT id, email, display_name FROM users WHERE id != ? ORDER BY id", (user["id"],))
    return page(request, "duels.html", incoming=incoming, active=active, history=history,
                my_decks=my_decks, others=others, duel_stats=du.stats_for(user["id"]))


@app.post("/duels/challenge")
async def create_challenge(request: Request, opponent_id: int = Form(...), deck_id: int = Form(...)):
    user = require_user(request)
    deck = owned_deck(user, deck_id)
    if not deck or deck["status"] != "ready":
        return RedirectResponse("/duels", status_code=303)
    opp = db.query_one("SELECT id FROM users WHERE id = ?", (opponent_id,))
    if not opp or opp["id"] == user["id"]:
        return RedirectResponse("/duels", status_code=303)
    from . import duels as du
    duel_id = du.new_duel(user["id"], opp["id"], deck_id)
    return RedirectResponse(f"/duels/{duel_id}", status_code=303)


@app.post("/duels/{duel_id}/accept")
async def accept_duel(request: Request, duel_id: str):
    user = require_user(request)
    from . import duels as du
    duel = du.get_duel(duel_id)
    if not duel or duel["opponent_id"] != user["id"] or duel["status"] != "pending":
        return RedirectResponse("/duels", status_code=303)
    db.execute("UPDATE duels SET status='active', started_at=datetime('now'),"
               " challenger_last_active=datetime('now'), opponent_last_active=datetime('now') WHERE id=?",
               (duel_id,))
    return RedirectResponse(f"/duels/{duel_id}/play", status_code=303)


@app.post("/duels/{duel_id}/decline")
async def decline_duel(request: Request, duel_id: str):
    user = require_user(request)
    from . import duels as du
    duel = du.get_duel(duel_id)
    if not duel or duel["opponent_id"] != user["id"] or duel["status"] != "pending":
        return RedirectResponse("/duels", status_code=303)
    du.finish_duel(duel_id, duel["challenger_id"])  # declined = challenger wins by forfeit
    return RedirectResponse("/duels", status_code=303)


@app.get("/duels/{duel_id}", response_class=HTMLResponse)
async def duel_lobby(request: Request, duel_id: str):
    user = require_user(request)
    from . import duels as du
    duel = du.get_duel(duel_id)
    if not duel or not du.is_participant(duel, user["id"]):
        return RedirectResponse("/duels", status_code=303)
    deck = db.query_one("SELECT title FROM decks WHERE id = ?", (duel["deck_id"],))
    opp = db.query_one("SELECT id, display_name, email FROM users WHERE id = ?",
                       (duel["opponent_id"] if duel["challenger_id"] == user["id"] else duel["challenger_id"],))
    challenger = db.query_one("SELECT id, display_name, email FROM users WHERE id = ?", (duel["challenger_id"],))
    return page(request, "duel_lobby.html", duel=duel, deck=deck, opp=opp, challenger=challenger,
                am_challenger=duel["challenger_id"] == user["id"])


@app.get("/duels/{duel_id}/play", response_class=HTMLResponse)
async def duel_play(request: Request, duel_id: str):
    user = require_user(request)
    from . import duels as du
    duel = du.get_duel(duel_id)
    if not duel or not du.is_participant(duel, user["id"]) or duel["status"] != "active":
        return RedirectResponse(f"/duels/{duel_id}", status_code=303)
    winner = du.check_forfeit(duel)
    if winner is not None:
        du.finish_duel(duel_id, winner)
        return RedirectResponse(f"/duels/{duel_id}/result", status_code=303)
    qids = du.duel_questions(duel["deck_id"], duel["seed"])
    state = du.duel_state_for(duel, user["id"])
    if state["my_idx"] >= du.DUEL_QUESTIONS:
        return RedirectResponse(f"/duels/{duel_id}/waiting", status_code=303)
    q = db.query_one("SELECT * FROM questions WHERE id = ?", (qids[state["my_idx"]],))
    if not q:
        return RedirectResponse(f"/duels/{duel_id}/waiting", status_code=303)
    # touch activity
    side = state["side"]
    db.execute(f"UPDATE duels SET {side}_last_active = ? WHERE id = ?", (db.now_utc(), duel_id))
    deck = db.query_one("SELECT title FROM decks WHERE id = ?", (duel["deck_id"],))
    return page(request, "duel_play.html", duel=duel, deck=deck,
                q={**dict(q), "options": json.loads(q["options_json"])},
                state=state, q_index=state["my_idx"], q_total=du.DUEL_QUESTIONS)


@app.post("/duels/{duel_id}/answer")
async def duel_answer(request: Request, duel_id: str, question_id: int = Form(...), choice: int = Form(...)):
    user = require_user(request)
    from . import duels as du
    duel = du.get_duel(duel_id)
    if not duel or not du.is_participant(duel, user["id"]) or duel["status"] != "active":
        return RedirectResponse(f"/duels/{duel_id}", status_code=303)
    qids = du.duel_questions(duel["deck_id"], duel["seed"])
    state = du.duel_state_for(duel, user["id"])
    if state["my_idx"] >= len(qids) or qids[state["my_idx"]] != question_id:
        return RedirectResponse(f"/duels/{duel_id}/play", status_code=303)
    res = du.record_answer(duel_id, user["id"], question_id, choice)
    # fragment: correctness + progress; polling fragment does the rest
    q = db.query_one("SELECT * FROM questions WHERE id = ?", (question_id,))
    return page(request, "_duel_feedback.html", duel=duel, res=res,
                q={**dict(q), "options": json.loads(q["options_json"])}, choice=choice,
                state=du.duel_state_for(du.get_duel(duel_id), user["id"]))


@app.get("/duels/{duel_id}/status", response_class=HTMLResponse)
async def duel_status(request: Request, duel_id: str):
    """Polling fragment: live scores + indexes for both players."""
    user = require_user(request)
    from . import duels as du
    duel = du.get_duel(duel_id)
    if not duel or not du.is_participant(duel, user["id"]):
        return HTMLResponse("")
    winner = du.check_forfeit(duel)
    if winner is not None and duel["status"] == "active":
        du.finish_duel(duel_id, winner)
        duel = du.get_duel(duel_id)
    if duel["status"] == "finished":
        return HTMLResponse("", headers={"HX-Redirect": f"/duels/{duel_id}/result"})
    state = du.duel_state_for(duel, user["id"])
    return page(request, "_duel_status.html", duel=duel, state=state, q_total=du.DUEL_QUESTIONS)


@app.get("/duels/{duel_id}/waiting", response_class=HTMLResponse)
async def duel_waiting(request: Request, duel_id: str):
    """You finished your 10; poll until opponent finishes or forfeits."""
    user = require_user(request)
    from . import duels as du
    duel = du.get_duel(duel_id)
    if not duel or not du.is_participant(duel, user["id"]):
        return RedirectResponse("/duels", status_code=303)
    side = "challenger" if duel["challenger_id"] == user["id"] else "opponent"
    db.execute(f"UPDATE duels SET {side}_last_active = ? WHERE id = ?", (db.now_utc(), duel_id))
    state = du.duel_state_for(duel, user["id"])
    deck = db.query_one("SELECT title FROM decks WHERE id = ?", (duel["deck_id"],))
    return page(request, "duel_waiting.html", duel=duel, deck=deck, state=state)


@app.get("/duels/{duel_id}/result", response_class=HTMLResponse)
async def duel_result(request: Request, duel_id: str):
    user = require_user(request)
    from . import duels as du
    duel = du.get_duel(duel_id)
    if not duel or not du.is_participant(duel, user["id"]):
        return RedirectResponse("/duels", status_code=303)
    if duel["status"] == "active":
        winner = du.check_forfeit(duel)
        if winner is not None:
            du.finish_duel(duel_id, winner)
            duel = du.get_duel(duel_id)
        elif all(duel[f"{s}_idx"] >= du.DUEL_QUESTIONS for s in ("challenger", "opponent")):
            # both finished: higher score wins, tie -> challenger
            w = duel["challenger_id"] if duel["challenger_score"] >= duel["opponent_score"] else duel["opponent_id"]
            du.finish_duel(duel_id, w)
            duel = du.get_duel(duel_id)
    if duel["status"] != "finished":
        return RedirectResponse(f"/duels/{duel_id}/play", status_code=303)
    deck = db.query_one("SELECT title FROM decks WHERE id = ?", (duel["deck_id"],))
    rows = db.query("SELECT user_id, COUNT(*) n, SUM(correct) ok FROM duel_answers WHERE duel_id = ? GROUP BY user_id",
                    (duel_id,))
    per_player = {}
    for r in rows:
        per_player[r["user_id"]] = {"answered": r["n"], "correct": r["ok"],
                                     "accuracy": round(100.0 * r["ok"] / r["n"]) if r["n"] else 0}
    names = {u["id"]: (u["display_name"] or u["email"]) for u in db.query(
        "SELECT id, display_name, email FROM users WHERE id IN (?, ?)", (duel["challenger_id"], duel["opponent_id"]))}
    return page(request, "duel_result.html", duel=duel, deck=deck, per_player=per_player, names=names,
                am_winner=duel["winner_id"] == user["id"], xp_bonus=du.DUEL_XP_BONUS)


# ---------- study ----------

@app.get("/study", response_class=HTMLResponse)
async def study_pick(request: Request):
    user = require_user(request)
    decks = db.query(
        "SELECT d.id, d.title, (SELECT COUNT(*) FROM questions q WHERE q.deck_id = d.id AND q.due_at <= datetime('now') AND q.flagged = 0) AS due"
        " FROM decks d WHERE d.user_id = ? AND d.status = 'ready' ORDER BY d.title", (user["id"],))
    return page(request, "study_pick.html", decks=decks)


def _current_session(user: dict, deck_id: int):
    """The open study_sessions row for this user+deck, or None. A session is
    closed after 30 minutes of inactivity (checked on page load)."""
    sess = db.query_one(
        "SELECT * FROM study_sessions WHERE user_id = ? AND deck_id = ? AND ended_at IS NULL"
        " ORDER BY started_at DESC LIMIT 1", (user["id"], deck_id))
    if sess and sess["started_at"]:
        from datetime import datetime, timedelta
        # close on 30 min of inactivity: use the latest attempt in the session
        last_att = db.query_one(
            "SELECT MAX(answered_at) a FROM attempts WHERE user_id = ? AND session_id = ?",
            (user["id"], sess["id"]))["a"]
        ref = last_att or sess["started_at"]
        ref_dt = datetime.fromisoformat(ref.replace("Z", "+00:00")) if "T" in ref \
            else datetime.strptime(ref, "%Y-%m-%d %H:%M:%S")
        if datetime.now(timezone.utc).replace(tzinfo=None) - ref_dt.replace(tzinfo=None) > timedelta(minutes=30):
            db.execute("UPDATE study_sessions SET ended_at = datetime('now') WHERE id = ?", (sess["id"],))
            return None
    return sess


@app.get("/study/{deck_id}", response_class=HTMLResponse)
async def study_session(request: Request, deck_id: int):
    user = require_user(request)
    deck = db.query_one("SELECT * FROM decks WHERE id = ? AND user_id = ?", (deck_id, user["id"]))
    if not deck:
        return RedirectResponse("/study", status_code=303)
    now = db.now_utc()
    due = db.query_one(
        "SELECT COUNT(*) c, MIN(due_at) next_due FROM questions WHERE deck_id = ? AND due_at > ?",
        (deck_id, now))
    sess = _current_session(user, deck_id)
    queue = _session_queue(user, deck_id, sess)
    if not queue:
        return page(request, "study_done.html", deck=deck, next_due=due["next_due"])
    q = queue[0]
    due_count = len(queue)
    combo = sess["combo"] if sess else 0
    return page(request, "study_card.html", deck=deck,
                q={**dict(q), "options": json.loads(q["options_json"])},
                due_count=due_count, est_min=max(1, round(due_count * 0.4)),
                combo=combo)


def _session_queue(user: dict, deck_id: int, sess) -> list:
    """Due, unflagged questions ordered least-mastered first (box asc), ties by
    longest time since last answered. Questions answered wrong in this session
    are excluded (they return in later sessions via their lowered box)."""
    wrong_ids = set()
    if sess and sess["wrong_ids"]:
        wrong_ids = {int(x) for x in sess["wrong_ids"].split(",") if x}
    rows = db.query(
        "SELECT q.*, COALESCE(m.box_level, 0) AS box, m.last_answered_at AS last_answered"
        " FROM questions q LEFT JOIN mastery m ON m.question_id = q.id AND m.user_id = ?"
        " WHERE q.deck_id = ? AND q.due_at <= ? AND q.flagged = 0"
        " ORDER BY box ASC, last_answered ASC, q.id ASC",
        (user["id"], deck_id, db.now_utc()))
    return [r for r in rows if r["id"] not in wrong_ids]




@app.post("/study/{deck_id}/answer")
async def study_answer(request: Request, deck_id: int, question_id: int = Form(...),
                       choice: int = Form(...)):
    """Show the graded choice. Scheduling happens once, in /rate."""
    user = require_user(request)
    q = owned_question(user, deck_id, question_id)
    if not q:
        return RedirectResponse(f"/study/{deck_id}", status_code=303)
    is_correct = choice == q["correct_index"]
    xp_preview = xp_for("good", True, q["difficulty"]) if is_correct else 1
    return page(request, "_answer_result.html", q={**dict(q), "options": json.loads(q["options_json"])},
                choice=choice, correct=1 if is_correct else 0, xp=xp_preview, deck_id=deck_id,
                flagged=bool(q["flagged"]), default_rating="good" if is_correct else "again")


@app.post("/study/{deck_id}/rate")
async def study_rate(request: Request, deck_id: int, question_id: int = Form(...),
                     choice: int = Form(...), rating: str = Form(...)):
    """Apply the rating (default good/again), award XP, render the next card or end session."""
    user = require_user(request)
    q = owned_question(user, deck_id, question_id)
    if not q:
        return RedirectResponse(f"/study/{deck_id}", status_code=303)
    is_correct = choice == q["correct_index"]
    if rating not in ("hard", "good", "easy") or not is_correct:
        rating = "again" if not is_correct else "good"
    return _finish_card(request, user, deck_id, q, choice, is_correct, rating)


def _finish_card(request, user, deck_id, q, choice, is_correct, rating=None):
    """Shared tail of /answer and /rate: schedule, award, render next card or summary."""
    correct = 1 if is_correct else 0
    if not is_correct:
        rating = "again"
    elif rating not in ("hard", "good", "easy"):
        rating = "good"
    new = grade(dict(q), rating)
    db.execute(
        "UPDATE questions SET due_at=?, interval_days=?, ef=?, reps=?, lapses=? WHERE id=?",
        (new["due_at"], new["interval_days"], new["ef"], new["reps"], new["lapses"], q["id"]))
    xp = xp_for(rating, bool(correct), q["difficulty"]) if correct else 1
    db.execute(
        "INSERT INTO reviews(question_id, user_id, rating, correct, xp_awarded, interval_days)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (q["id"], user["id"], rating, correct, xp, new["interval_days"]))
    total = db.query_one("SELECT COUNT(*) c FROM reviews WHERE user_id = ?", (user["id"],))["c"]
    award = gamify.award_xp(user["id"], xp)
    activity = gamify.touch_activity(user["id"])
    badges = gamify.check_badges(user["id"], total)
    sess = _current_session(user, deck_id)
    if not sess:
        cur = db.execute("INSERT INTO study_sessions(user_id, deck_id) VALUES (?, ?)",
                         (user["id"], deck_id))
        sess_id = cur.lastrowid
    else:
        sess_id = sess["id"]
    # mastery: log exactly one attempt, move the box, update combo
    from . import mastery as _mastery
    _mastery.record_attempt(user["id"], q["id"], deck_id, bool(correct), 0, sess_id)
    combo = (sess["combo"] if sess else 0) + (1 if correct else 0)
    if not correct:
        combo = 0
        wrong = (sess["wrong_ids"] if sess else "") if sess else ""
        wrong = (wrong + "," if wrong else "") + str(q["id"])
        db.execute("UPDATE study_sessions SET wrong_ids = ? WHERE id = ?", (wrong, sess_id))
    best_combo = max(sess["best_combo"] if sess else 0, combo)
    if combo > (db.query_one("SELECT biggest_combo c FROM users WHERE id = ?", (user["id"],))["c"] or 0):
        db.execute("UPDATE users SET biggest_combo = ? WHERE id = ?", (combo, user["id"]))
    db.execute(
        "UPDATE study_sessions SET cards_reviewed = cards_reviewed + 1, xp_earned = xp_earned + ?,"
        " combo = ?, best_combo = ? WHERE id = ?",
        (xp, combo, best_combo, sess_id))
    streak_info = _mastery.touch_streak(user["id"])
    new_badges = _mastery.award_badges(user["id"], deck_id=deck_id)
    queue = _session_queue(user, deck_id, db.query_one("SELECT * FROM study_sessions WHERE id = ?", (sess_id,)))
    nxt = queue[0] if queue else None
    if nxt:
        deck = db.query_one("SELECT * FROM decks WHERE id = ?", (deck_id,))
        now = db.now_utc()
        due_count = len(queue)
        return page(request, "_card_body.html", deck=deck,
                    q={**dict(nxt), "options": json.loads(nxt["options_json"])},
                    due_count=due_count, est_min=max(1, round(due_count * 0.4)),
                    last_correct=bool(correct), last_xp=xp, combo=combo,
                    award=award, activity=activity, badges=badges + new_badges)
    sess_row = db.query_one("SELECT * FROM study_sessions WHERE id = ?", (sess_id,))
    db.execute("UPDATE study_sessions SET ended_at = datetime('now') WHERE id = ?", (sess_id,))
    acc = db.query_one("SELECT ROUND(100.0*SUM(correct)/COUNT(*),1) a FROM reviews WHERE user_id=? AND id IN (SELECT id FROM reviews WHERE reviewed_at >= ?)", (user["id"], sess_row["started_at"]))
    perfect = sess_row["cards_reviewed"] >= 10 and sess_row["combo"] == sess_row["cards_reviewed"]
    new_badges += _mastery.award_badges(user["id"], deck_id=deck_id,
                                        session={"cards_reviewed": sess_row["cards_reviewed"],
                                                 "combo": sess_row["combo"]})
    deck = db.query_one("SELECT * FROM decks WHERE id = ?", (deck_id,))
    next_due = db.query_one("SELECT MIN(due_at) d FROM questions WHERE deck_id = ? AND due_at > ?",
                            (deck_id, db.now_utc()))["d"]
    if request.headers.get("hx-request"):
        resp = Response(status_code=200)
        resp.headers["HX-Redirect"] = f"/study/{deck_id}/done"
        return resp
    return page(request, "study_summary.html", deck=deck, correct=correct, xp=xp,
                cards=sess_row["cards_reviewed"], xp_total=sess_row["xp_earned"],
                accuracy=acc["a"] or 0, award=award, activity=activity, badges=badges + new_badges,
                next_due=next_due, combo=combo, best_combo=sess_row["best_combo"],
                streak=streak_info["streak"])


@app.get("/study/{deck_id}/done", response_class=HTMLResponse)
async def study_done(request: Request, deck_id: int):
    """Session summary page (target of the htmx redirect at session end)."""
    user = require_user(request)
    deck = db.query_one("SELECT * FROM decks WHERE id = ? AND user_id = ?", (deck_id, user["id"]))
    if not deck:
        return RedirectResponse("/study", status_code=303)
    sess = db.query_one(
        "SELECT * FROM study_sessions WHERE user_id = ? AND deck_id = ? ORDER BY started_at DESC LIMIT 1",
        (user["id"], deck_id))
    if not sess:
        return RedirectResponse(f"/study/{deck_id}", status_code=303)
    acc = db.query_one(
        "SELECT ROUND(100.0*SUM(correct)/COUNT(*),1) a FROM reviews WHERE user_id=? AND id IN (SELECT id FROM reviews WHERE reviewed_at >= ?)",
        (user["id"], sess["started_at"]))
    next_due = db.query_one("SELECT MIN(due_at) d FROM questions WHERE deck_id = ? AND due_at > ?",
                            (deck_id, db.now_utc()))["d"]
    return page(request, "study_summary.html", deck=deck, correct=1, xp=sess["xp_earned"],
                cards=sess["cards_reviewed"], xp_total=sess["xp_earned"],
                accuracy=acc["a"] or 0, award={}, activity={}, badges=[],
                next_due=next_due)


def _owned_question(user, qid: int):
    return db.query_one(
        "SELECT q.* FROM questions q JOIN decks d ON d.id = q.deck_id"
        " WHERE q.id = ? AND d.user_id = ?", (qid, user["id"]))


@app.post("/questions/{qid}/flag")
async def flag_question(request: Request, qid: int):
    user = require_user(request)
    row = _owned_question(user, qid)
    if not row:
        return RedirectResponse("/decks", status_code=303)
    db.execute("UPDATE questions SET flagged = 1 WHERE id = ?", (qid,))
    if request.headers.get("hx-request"):
        return HTMLResponse('<span class="chip chip-flame" role="status">' + translate(lang_of(request), "flagged_thanks") + "</span>")
    referer = request.headers.get("referer") or ""
    if not referer.startswith("/"):
        referer = "/decks"
    return RedirectResponse(referer + ("&" if "?" in referer else "?") + "flagged=1", status_code=303)


@app.post("/questions/{qid}/unflag")
async def unflag_question(request: Request, qid: int):
    user = require_user(request)
    row = _owned_question(user, qid)
    if not row:
        return RedirectResponse("/decks", status_code=303)
    db.execute("UPDATE questions SET flagged = 0 WHERE id = ?", (qid,))
    return RedirectResponse(f"/decks/{row['deck_id']}?flagged=0", status_code=303)


@app.post("/questions/{qid}/delete")
async def delete_question(request: Request, qid: int):
    user = require_user(request)
    row = _owned_question(user, qid)
    if not row:
        return RedirectResponse("/decks", status_code=303)
    db.execute("DELETE FROM questions WHERE id = ?", (qid,))
    return RedirectResponse(f"/decks/{row['deck_id']}/questions?deleted=1", status_code=303)


@app.post("/questions/{qid}/edit")
async def edit_question(request: Request, qid: int, prompt: str = Form(...),
                        opt0: str = Form(""), opt1: str = Form(""), opt2: str = Form(""),
                        opt3: str = Form(""), correct: int = Form(0),
                        explanation: str = Form("")):
    """User edits a question. Sets edited=1 so regen 'add' never dedups it away."""
    user = require_user(request)
    q = _owned_question(user, qid)
    if not q:
        return RedirectResponse("/decks", status_code=303)
    prompt = prompt.strip()
    options = [o.strip() for o in (opt0, opt1, opt2, opt3)]
    if len(prompt) < 10 or not all(options) or len(set(o.lower() for o in options)) < 4:
        return RedirectResponse(f"/decks/{q['deck_id']}/questions?error=invalid", status_code=303)
    if not (0 <= correct <= 3):
        correct = 0
    db.execute(
        "UPDATE questions SET prompt=?, options_json=?, correct_index=?, explanation=?, edited=1 WHERE id=?",
        (prompt[:400], json.dumps(options), correct, explanation.strip()[:600], qid))
    return RedirectResponse(f"/decks/{q['deck_id']}/questions?edited=1", status_code=303)


@app.post("/questions/{qid}/regen")
async def regen_question(request: Request, qid: int):
    """Regenerate a single question: re-run generate+verify on its chunk and
    replace the row (keeps the id stable for review history)."""
    user = require_user(request)
    q = _owned_question(user, qid)
    if not q:
        return RedirectResponse("/decks", status_code=303)
    from .generation import regenerate_single_question
    ok = regenerate_single_question(user["id"], q["deck_id"], qid)
    return RedirectResponse(
        f"/decks/{q['deck_id']}/questions?" + ("regen=1" if ok else "regen=0"), status_code=303)


@app.post("/decks/{deck_id}/regen")
async def regen_deck(request: Request, deck_id: int, mode: str = Form("replace")):
    """mode=replace (default): delete all questions and regenerate from scratch.
    mode=add: keep existing questions; the job dedups new drafts against them."""
    user = require_user(request)
    if mode not in ("replace", "add"):
        mode = "replace"
    deck = db.query_one(
        "SELECT d.*, s.kind AS source_kind, s.raw_text FROM decks d"
        " JOIN sources s ON s.id = d.source_id WHERE d.id = ? AND d.user_id = ?",
        (deck_id, user["id"]))
    if not deck:
        return RedirectResponse("/decks", status_code=303)
    active = db.query_one(
        "SELECT 1 FROM jobs WHERE deck_id = ? AND status IN ('queued','running')", (deck_id,))
    if active:
        return RedirectResponse(f"/decks/{deck_id}", status_code=303)
    if mode == "replace":
        db.execute("DELETE FROM questions WHERE deck_id = ?", (deck_id,))
    db.execute("DELETE FROM chunks WHERE source_id = ?", (deck["source_id"],))
    # URL decks re-crawl in the job. File/text decks have no crawler: re-chunk
    # from the stored raw_text, otherwise regen yields zero chunks/questions.
    if deck["source_kind"] != "url":
        for idx, piece in enumerate(chunk_text_v2(deck["raw_text"] or "")):
            db.execute("INSERT INTO chunks(source_id, idx, text) VALUES (?, ?, ?)",
                       (deck["source_id"], idx, piece))
    db.execute("UPDATE decks SET status='draft' WHERE id = ?", (deck_id,))
    job_id = start_generation(user["id"], deck_id, mode=mode)
    log.info("deck %s regen mode=%s (user=%s), job %s started", deck_id, mode, user["id"], job_id)
    return RedirectResponse(f"/decks/{deck_id}?job={job_id}", status_code=303)


@app.post("/decks/{deck_id}/rename")
async def rename_deck(request: Request, deck_id: int, title: str = Form(...)):
    user = require_user(request)
    title = title.strip()
    deck = db.query_one("SELECT * FROM decks WHERE id = ? AND user_id = ?", (deck_id, user["id"]))
    if not deck or not title:
        return RedirectResponse(f"/decks/{deck_id}", status_code=303)
    db.execute("UPDATE decks SET title = ? WHERE id = ?", (title[:120], deck_id))
    return RedirectResponse(f"/decks/{deck_id}?msg=1", status_code=303)


@app.post("/decks/{deck_id}/delete")
async def delete_deck(request: Request, deck_id: int):
    user = require_user(request)
    db.execute("DELETE FROM job_events WHERE job_id IN (SELECT id FROM jobs WHERE deck_id = ?)",
               (deck_id,))
    db.execute("DELETE FROM decks WHERE id = ? AND user_id = ?", (deck_id, user["id"]))
    return RedirectResponse("/decks", status_code=303)


@app.post("/settings/streaks")
async def toggle_streaks(request: Request, hide: int = Form(0)):
    user = require_user(request)
    db.execute("UPDATE users SET hide_streaks = ? WHERE id = ?", (hide, user["id"]))
    return RedirectResponse("/settings", status_code=303)


# ---------- stats ----------

@app.get("/stats", response_class=HTMLResponse)
async def stats(request: Request):
    user = require_user(request)
    total_q = db.query_one("SELECT COUNT(*) c FROM questions q JOIN decks d ON d.id = q.deck_id WHERE d.user_id = ?",
                           (user["id"],))["c"]
    total_r = db.query_one("SELECT COUNT(*) c FROM reviews WHERE user_id = ?", (user["id"],))["c"]
    acc = db.query_one(
        "SELECT ROUND(100.0 * SUM(correct) / COUNT(*), 1) a FROM reviews WHERE user_id = ?", (user["id"],))["a"]
    badges = db.query("SELECT * FROM user_badges WHERE user_id = ? ORDER BY awarded_at", (user["id"],))
    recent = db.query(
        "SELECT DATE(reviewed_at) d, COUNT(*) c FROM reviews WHERE user_id = ?"
        " GROUP BY d ORDER BY d DESC LIMIT 14", (user["id"],))
    from . import duels as du
    return page(request, "stats.html", total_q=total_q, total_r=total_r, accuracy=acc or 0,
                badges=badges, recent=recent, all_badges=list(gamify.BADGES),
                duel_stats=du.stats_for(user["id"]))


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    """Profile: stats summary, biggest combo, streak, and the full badge list
    (earned with dates, locked grayed out with how-to-earn)."""
    user = require_user(request)
    from . import mastery as _mastery
    badges = db.query("SELECT * FROM user_badges WHERE user_id = ? ORDER BY awarded_at", (user["id"],))
    biggest_combo = user["biggest_combo"] if "biggest_combo" in user.keys() else 0
    return page(request, "profile.html",
                badges=badges, all_badges=list(_mastery.BADGES), biggest_combo=biggest_combo,
                streak=user["streak_days"])


# ---------- admin ----------

def require_admin(request: Request):
    user = require_user(request)
    if user["role"] != "admin":
        raise _Redirect("/decks")
    return user


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, msg: str | None = None, error: str | None = None):
    user = require_admin(request)
    users = db.query("SELECT id, email, display_name, role, created_at FROM users ORDER BY id")
    from . import diagnostics
    diag = diagnostics.run_all_checks()
    jobs = db.query(
        "SELECT j.id, j.status, j.total, j.done, j.error, j.created_at, j.finished_at,"
        " d.title AS deck_title, d.id AS deck_id, u.email AS user_email"
        " FROM jobs j JOIN decks d ON d.id = j.deck_id JOIN users u ON u.id = j.user_id"
        " ORDER BY j.created_at DESC LIMIT 20")
    return page(request, "admin.html", users=users, msg=msg, error=error,
                diagnostics=diag, jobs=jobs)


@app.get("/admin/jobs/{job_id}/events")
async def admin_job_events(request: Request, job_id: str):
    user = require_admin(request)
    events = db.query(
        "SELECT level, message, created_at FROM job_events WHERE job_id = ?"
        " ORDER BY id", (job_id,))
    job = db.query_one(
        "SELECT j.*, d.title AS deck_title FROM jobs j JOIN decks d ON d.id = j.deck_id"
        " WHERE j.id = ?", (job_id,))
    if not job:
        return RedirectResponse("/admin", status_code=303)
    return page(request, "job_events.html", job=job, events=events)


@app.post("/admin/users/{user_id}/role")
async def admin_set_role(request: Request, user_id: int, role: str = Form(...)):
    user = require_admin(request)
    if role not in ("member", "privileged", "admin"):
        return RedirectResponse("/admin?error=Invalid role", status_code=303)
    if user_id == user["id"] and role != "admin":
        return RedirectResponse("/admin?error=You cannot demote yourself", status_code=303)
    db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    return RedirectResponse("/admin?msg=Role updated", status_code=303)


# ---------- settings (LLM keys) ----------

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, msg: str | None = None, error: str | None = None):
    user = require_user(request)
    keys = db.query("SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at DESC", (user["id"],))
    us = db.query_one("SELECT * FROM user_settings WHERE user_id = ?", (user["id"],))
    providers = dict(DEFAULT_MODELS)
    if user["role"] in ("admin", "privileged") and settings.mistral_api_key:
        providers = {"builtin": [settings.builtin_model], **providers}
    return page(request, "settings.html",
                keys=[{**dict(k), "masked": mask_key(decrypt_secret(k["encrypted_key"]))} for k in keys],
                settings_row=us, providers=providers, msg=msg, error=error,
                builtin_available=bool(settings.mistral_api_key),
                settings=settings)


@app.post("/settings/keys")
async def add_key(request: Request, provider: str = Form(...), model: str = Form(...),
                  api_key: str = Form(...), label: str = Form("")):
    user = require_user(request)
    api_key = api_key.strip()
    if not api_key:
        return RedirectResponse("/settings?error=API key cannot be empty", status_code=303)
    cur = db.execute(
        "INSERT INTO api_keys(user_id, provider, label, encrypted_key) VALUES (?, ?, ?, ?)",
        (user["id"], provider, label.strip(), encrypt_secret(api_key)))
    db.execute(
        "UPDATE user_settings SET active_key_id = ?, default_provider = ?, default_model = ? WHERE user_id = ?",
        (cur.lastrowid, provider, model.strip(), user["id"]))
    return RedirectResponse("/settings?msg=API key saved and set as active", status_code=303)


@app.post("/settings/keys/{key_id}/activate")
async def activate_key(request: Request, key_id: int, provider: str = Form(...), model: str = Form(...)):
    user = require_user(request)
    db.execute("UPDATE user_settings SET active_key_id = ?, default_provider = ?, default_model = ? WHERE user_id = ?",
               (key_id, provider, model.strip(), user["id"]))
    return RedirectResponse("/settings?msg=Active model updated", status_code=303)


@app.post("/settings/keys/{key_id}/delete")
async def delete_key(request: Request, key_id: int):
    user = require_user(request)
    db.execute("DELETE FROM api_keys WHERE id = ? AND user_id = ?", (key_id, user["id"]))
    db.execute("UPDATE user_settings SET active_key_id = NULL WHERE user_id = ? AND active_key_id = ?",
               (user["id"], key_id))
    return RedirectResponse("/settings?msg=API key deleted", status_code=303)


@app.post("/settings/model")
async def set_model(request: Request, provider: str = Form(...), model: str = Form(...)):
    user = require_user(request)
    db.execute("UPDATE user_settings SET default_provider = ?, default_model = ? WHERE user_id = ?",
               (provider, model.strip(), user["id"]))
    return RedirectResponse("/settings?msg=Default model updated", status_code=303)


# ---------- passkeys (registered only when enabled) ----------

if settings.passkeys_enabled:
    from . import passkeys as pk

    @app.get("/passkeys/register-options", response_class=HTMLResponse)
    async def passkey_register_options(request: Request):
        import json as _json
        user = require_user(request)
        return Response(_json.dumps(pk.registration_options(user)), media_type="application/json")

    @app.post("/passkeys/register")
    async def passkey_register(request: Request):
        import json as _json
        user = require_user(request)
        body = await request.json()
        pk.complete_registration(user, body)
        return Response(_json.dumps({"ok": True}), media_type="application/json")

    @app.get("/passkeys/login-options", response_class=HTMLResponse)
    async def passkey_login_options(request: Request):
        import json as _json
        return Response(_json.dumps(pk.authentication_options()), media_type="application/json")

    @app.post("/passkeys/login")
    async def passkey_login(request: Request):
        import json as _json
        body = await request.json()
        user = pk.complete_authentication(body)
        token = create_session(user["id"])
        resp = Response(_json.dumps({"ok": True}), media_type="application/json")
        resp.set_cookie("quipeat_session", token, httponly=True, samesite="lax", max_age=30 * 86400)
        return resp
