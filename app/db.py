import sqlite3
import threading
from pathlib import Path

from .config import settings

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(settings.database_path, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
        return _conn


def init_db() -> None:
    schema = Path(__file__).parent / "schema.sql"
    with _lock:
        conn = get_conn()
        conn.executescript(schema.read_text())
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "onboarded_at" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN onboarded_at TEXT")
        if "role" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
        if "lang" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN lang TEXT NOT NULL DEFAULT 'en'")
        if "freeze_available" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN freeze_available INTEGER NOT NULL DEFAULT 1")
        if "hide_streaks" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN hide_streaks INTEGER NOT NULL DEFAULT 0")
        qcols = {r["name"] for r in conn.execute("PRAGMA table_info(questions)").fetchall()}
        if "flagged" not in qcols:
            conn.execute("ALTER TABLE questions ADD COLUMN flagged INTEGER NOT NULL DEFAULT 0")
        dcols = {r["name"] for r in conn.execute("PRAGMA table_info(decks)").fetchall()}
        if "lang" not in dcols:
            conn.execute("ALTER TABLE decks ADD COLUMN lang TEXT NOT NULL DEFAULT 'en'")
        if "level" not in dcols:
            conn.execute("ALTER TABLE decks ADD COLUMN level TEXT NOT NULL DEFAULT 'standard'")
        conn.commit()
        _ensure_admin(conn)
        conn.commit()


def _ensure_admin(conn: sqlite3.Connection) -> None:
    from .config import settings
    ident = settings.admin_ident.strip()
    if ident:
        row = conn.execute(
            "SELECT id FROM users WHERE lower(email) = lower(?) OR lower(display_name) = lower(?) LIMIT 1",
            (ident, ident)).fetchone()
        if row:
            conn.execute("UPDATE users SET role = 'member' WHERE role = 'admin' AND id != ?", (row["id"],))
            conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (row["id"],))
            return
    if not conn.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone():
        row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if row:
            conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (row["id"],))


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    with _lock:
        return get_conn().execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = get_conn().execute(sql, params)
        get_conn().commit()
        return cur
