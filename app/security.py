import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from cryptography.fernet import Fernet

from .config import settings
from . import db

ph = PasswordHasher()


def hash_password(password: str) -> str:
    return ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return ph.verify(password_hash, password)
    except Exception:
        return False


def _fernet() -> Fernet:
    if settings.master_key:
        return Fernet(settings.master_key.encode())
    digest = hashlib.sha256(settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def create_session(user_id: int, days: int = 30) -> str:
    token = new_session_token()
    expires = datetime.now(timezone.utc) + timedelta(days=days)
    db.execute(
        "INSERT INTO sessions(token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user_id, expires.strftime("%Y-%m-%d %H:%M:%S")),
    )
    return token


def get_session_user(token: str | None):
    if not token:
        return None
    row = db.query_one(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ? AND s.expires_at > ?",
        (token, db.now_utc()),
    )
    return row


def prune_expired_sessions() -> int:
    cur = db.execute("DELETE FROM sessions WHERE expires_at <= ?", (db.now_utc(),))
    return cur.rowcount


def delete_session(token: str) -> None:
    db.execute("DELETE FROM sessions WHERE token = ?", (token,))


def mask_key(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"
