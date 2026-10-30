import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, Response, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, gamify
from .config import settings
from .fsrs import grade, xp_for
from .generation import start_generation
from .ingest import chunk_text, extract_text_from_file, extract_text_from_url, IngestError
from .llm.providers import DEFAULT_MODELS
from .security import (create_session, decrypt_secret, delete_session, encrypt_secret,
                       get_session_user, hash_password, mask_key, verify_password)

BASE_DIR = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

app = FastAPI(title="Quipeat")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


@app.on_event("startup")
def _startup():
    db.init_db()
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)


def current_user(request: Request):
    return get_session_user(request.cookies.get("quipeat_session"))


def require_user(request: Request):
    user = current_user(request)
    if not user:
        raise _Redirect("/login")
    return user


class _Redirect(Exception):
    def __init__(self, location: str):
        self.location = location


@app.exception_handler(_Redirect)
async def _redirect_handler(request: Request, exc: _Redirect):
    return RedirectResponse(exc.location, status_code=303)


def page(request: Request, template: str, **ctx) -> HTMLResponse:
    ctx.setdefault("request", request)
    ctx.setdefault("user", current_user(request))
    ctx.setdefault("passkeys_enabled", settings.passkeys_enabled)
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
    if len(password) < 8:
        return page(request, "register.html", error="Password must be at least 8 characters.")
    if db.query_one("SELECT 1 FROM users WHERE email = ?", (email,)):
        return page(request, "register.html", error="An account with this email already exists.")
    cur = db.execute("INSERT INTO users(email, password_hash, display_name) VALUES (?, ?, ?)",
                     (email, hash_password(password), display_name.strip()))
    db.execute("INSERT INTO user_settings(user_id) VALUES (?)", (cur.lastrowid,))
    token = create_session(cur.lastrowid)
    resp = RedirectResponse("/onboarding", status_code=303)
    resp.set_cookie("quipeat_session", token, httponly=True, samesite="lax", max_age=30 * 86400)
    return resp


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str | None = None):
    return page(request, "login.html", error=error)


@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    user = db.query_one("SELECT * FROM users WHERE email = ?", (email,))
    if not user or not verify_password(password, user["password_hash"]):
        return page(request, "login.html", error="Invalid email or password.")
    token = create_session(user["id"])
    resp = RedirectResponse("/onboarding" if not user["onboarded_at"] else "/decks", status_code=303)
    resp.set_cookie("quipeat_session", token, httponly=True, samesite="lax", max_age=30 * 86400)
    return resp


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding(request: Request, step: int = 1):
    user = require_user(request)
    if user["onboarded_at"]:
        return RedirectResponse("/decks", status_code=303)
    step = max(1, min(4, step))
    return page(request, "onboarding.html", step=step, total_steps=4)


@app.post("/onboarding/finish")
async def onboarding_finish(request: Request):
    user = require_user(request)
    db.execute("UPDATE users SET onboarded_at = datetime('now') WHERE id = ?", (user["id"],))
    return RedirectResponse("/decks", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get("quipeat_session")
    if token:
        delete_session(token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("quipeat_session")
    return resp


# ---------- decks & sources ----------

@app.get("/decks", response_class=HTMLResponse)
async def decks(request: Request):
    user = require_user(request)
    rows = db.query(
        "SELECT d.*, s.kind AS source_kind, (SELECT COUNT(*) FROM questions q WHERE q.deck_id = d.id) AS q_count,"
        " (SELECT COUNT(*) FROM questions q WHERE q.deck_id = d.id AND q.due_at <= datetime('now')) AS due_count"
        " FROM decks d JOIN sources s ON s.id = d.source_id WHERE d.user_id = ? ORDER BY d.created_at DESC",
        (user["id"],))
    return page(request, "decks.html", decks=rows)


@app.get("/new", response_class=HTMLResponse)
async def new_source_form(request: Request):
    require_user(request)
    return page(request, "new_source.html", error=None)


@app.post("/new")
async def new_source(request: Request, title: str = Form(...), url: str = Form(""),
                     topic_text: str = Form(""), file: UploadFile | None = File(None)):
    user = require_user(request)
    title = title.strip()
    url = url.strip()
    topic_text = topic_text.strip()
    try:
        if file and file.filename:
            data = await file.read()
            if len(data) > settings.max_upload_mb * 1024 * 1024:
                raise IngestError(f"File exceeds {settings.max_upload_mb} MB limit.")
            tmp = Path(settings.upload_dir) / f"{secrets.token_hex(8)}_{file.filename}"
            tmp.write_bytes(data)
            raw = extract_text_from_file(tmp, file.filename)
            kind, ref = "file", file.filename
            tmp.unlink(missing_ok=True)
        elif url:
            raw = extract_text_from_url(url)
            kind, ref = "url", url
        elif topic_text:
            raw = topic_text
            kind, ref = "text", None
        else:
            raise IngestError("Provide a file, a URL, or some text about the topic.")
    except IngestError as e:
        return page(request, "new_source.html", error=str(e))
    if not raw.strip():
        return page(request, "new_source.html", error="No readable text found in the source.")
    if not title:
        title = (ref or "Untitled")[:80]
    cur = db.execute("INSERT INTO sources(user_id, kind, title, original_ref, raw_text) VALUES (?, ?, ?, ?, ?)",
                     (user["id"], kind, title, ref, raw))
    source_id = cur.lastrowid
    for idx, piece in enumerate(chunk_text(raw)):
        db.execute("INSERT INTO chunks(source_id, idx, text) VALUES (?, ?, ?)", (source_id, idx, piece))
    cur = db.execute("INSERT INTO decks(user_id, source_id, title) VALUES (?, ?, ?)",
                     (user["id"], source_id, title))
    deck_id = cur.lastrowid
    job_id = start_generation(user["id"], deck_id)
    return RedirectResponse(f"/decks/{deck_id}?job={job_id}", status_code=303)


@app.get("/decks/{deck_id}", response_class=HTMLResponse)
async def deck_detail(request: Request, deck_id: int, job: str | None = None):
    user = require_user(request)
    deck = db.query_one(
        "SELECT d.*, s.kind AS source_kind, s.original_ref, s.title AS source_title,"
        " (SELECT COUNT(*) FROM questions q WHERE q.deck_id = d.id) AS q_count,"
        " (SELECT COUNT(*) FROM questions q WHERE q.deck_id = d.id AND q.due_at <= datetime('now')) AS due_count"
        " FROM decks d JOIN sources s ON s.id = d.source_id WHERE d.id = ? AND d.user_id = ?",
        (deck_id, user["id"]))
    if not deck:
        return RedirectResponse("/decks", status_code=303)
    job_row = db.query_one("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job, user["id"])) if job else None
    return page(request, "deck_detail.html", deck=deck, job=job_row)


@app.get("/decks/{deck_id}/job-status", response_class=HTMLResponse)
async def job_status(request: Request, deck_id: int):
    user = require_user(request)
    job = db.query_one(
        "SELECT * FROM jobs WHERE deck_id = ? AND user_id = ? ORDER BY created_at DESC LIMIT 1",
        (deck_id, user["id"]))
    if not job:
        return HTMLResponse("")
    return templates.TemplateResponse(request, "_job_status.html", {
        "request": request, "job": job, "deck_id": deck_id, "user": user})


@app.get("/decks/{deck_id}/questions", response_class=HTMLResponse)
async def deck_questions(request: Request, deck_id: int):
    user = require_user(request)
    qs = db.query("SELECT * FROM questions WHERE deck_id = ? ORDER BY id", (deck_id,))
    items = [{**dict(q), "options": json.loads(q["options_json"])} for q in qs]
    deck = db.query_one("SELECT * FROM decks WHERE id = ? AND user_id = ?", (deck_id, user["id"]))
    return page(request, "questions.html", questions=items, deck=deck)


# ---------- study ----------

@app.get("/study", response_class=HTMLResponse)
async def study_pick(request: Request):
    user = require_user(request)
    decks = db.query(
        "SELECT d.id, d.title, (SELECT COUNT(*) FROM questions q WHERE q.deck_id = d.id AND q.due_at <= datetime('now')) AS due"
        " FROM decks d WHERE d.user_id = ? AND d.status = 'ready' ORDER BY d.title", (user["id"],))
    return page(request, "study_pick.html", decks=decks)


@app.get("/study/{deck_id}", response_class=HTMLResponse)
async def study_session(request: Request, deck_id: int):
    user = require_user(request)
    deck = db.query_one("SELECT * FROM decks WHERE id = ? AND user_id = ?", (deck_id, user["id"]))
    if not deck:
        return RedirectResponse("/study", status_code=303)
    q = db.query_one(
        "SELECT * FROM questions WHERE deck_id = ? AND due_at <= ? ORDER BY due_at LIMIT 1",
        (deck_id, datetime.now(timezone.utc).isoformat()))
    if not q:
        return page(request, "study_done.html", deck=deck)
    return page(request, "study_card.html", deck=deck, q={**dict(q), "options": json.loads(q["options_json"])})


@app.post("/study/{deck_id}/answer")
async def study_answer(request: Request, deck_id: int, question_id: int = Form(...),
                       choice: int = Form(...), rating: str = Form("good")):
    user = require_user(request)
    q = db.query_one("SELECT * FROM questions WHERE id = ? AND deck_id = ?", (question_id, deck_id))
    if not q:
        return RedirectResponse(f"/study/{deck_id}", status_code=303)
    correct = 1 if choice == q["correct_index"] else 0
    if not correct:
        rating = "again"
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
    sess = db.query_one(
        "SELECT * FROM study_sessions WHERE user_id = ? AND deck_id = ? AND ended_at IS NULL"
        " ORDER BY started_at DESC LIMIT 1", (user["id"], deck_id))
    if not sess:
        cur = db.execute("INSERT INTO study_sessions(user_id, deck_id) VALUES (?, ?)",
                         (user["id"], deck_id))
        sess_id = cur.lastrowid
    else:
        sess_id = sess["id"]
    db.execute("UPDATE study_sessions SET cards_reviewed = cards_reviewed + 1, xp_earned = xp_earned + ? WHERE id = ?",
               (xp, sess_id))
    return templates.TemplateResponse(request, "_answer_result.html", {
        "request": request, "user": user, "correct": correct, "xp": xp,
        "explanation": q["explanation"], "options": json.loads(q["options_json"]),
        "correct_index": q["correct_index"], "deck_id": deck_id,
        "award": award, "activity": activity, "badges": badges})


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
    return page(request, "stats.html", total_q=total_q, total_r=total_r, accuracy=acc or 0,
                badges=badges, recent=recent, all_badges=gamify.BADGES)


# ---------- settings (LLM keys) ----------

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, msg: str | None = None, error: str | None = None):
    user = require_user(request)
    keys = db.query("SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at DESC", (user["id"],))
    us = db.query_one("SELECT * FROM user_settings WHERE user_id = ?", (user["id"],))
    return page(request, "settings.html",
                keys=[{**dict(k), "masked": mask_key(decrypt_secret(k["encrypted_key"]))} for k in keys],
                settings_row=us, providers=DEFAULT_MODELS, msg=msg, error=error)


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
