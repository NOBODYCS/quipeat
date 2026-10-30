import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

# One connection per thread: SQLite connections are not safe to share across
# threads, and WAL mode lets threads read concurrently while writers serialize
# at the DB level. All access goes through _local; no global lock needed.
_local = threading.local()
_init_lock = threading.Lock()


def now_utc() -> str:
    """Current UTC time in the same format SQLite's datetime('now') produces,
    so Python-written timestamps compare correctly in SQL string comparisons."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _open() -> sqlite3.Connection:
    Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.database_path, check_same_thread=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # busy_timeout is essential: in WAL mode a COMMIT that finds the write lock
    # held fails with SQLITE_BUSY *without* honoring busy_timeout on some paths,
    # and python's legacy isolation_level opens implicit transactions, so a
    # long-running write elsewhere would otherwise surface as 'database is locked'.
    conn.execute("PRAGMA busy_timeout=30000")
    # Autocommit mode: each statement is its own transaction, so a connection
    # never sits inside an open write transaction between calls (which is what
    # made other threads fail with SQLITE_BUSY at COMMIT time).
    conn.isolation_level = None
    return conn


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _open()
        _local.conn = conn
    return conn


def init_db() -> None:
    schema = Path(__file__).parent / "schema.sql"
    with _init_lock:
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
        if "goal" not in dcols:
            conn.execute("ALTER TABLE decks ADD COLUMN goal TEXT NOT NULL DEFAULT ''")
        # Normalize Python-ISO timestamps ('...T...+00:00') written by older
        # versions of fsrs/security into SQLite's datetime('now') format so
        # string comparisons in SQL work uniformly.
        for table, col in (("questions", "due_at"), ("sessions", "expires_at"),
                           ("passkey_challenges", "expires_at")):
            conn.execute(
                f"UPDATE {table} SET {col} = substr(replace({col}, 'T', ' '), 1, 19)"
                f" WHERE {col} LIKE '%T%'")
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
    return get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return get_conn().execute(sql, params).fetchone()


_write_lock = threading.Lock()


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    # Serialize writers process-wide: with per-thread connections, SQLite's
    # cross-connection lock only helps so far; a short in-process write lock
    # keeps every statement atomic and avoids SQLITE_BUSY under bursts.
    with _write_lock:
        conn = get_conn()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur
