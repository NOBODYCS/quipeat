PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    xp_total      INTEGER NOT NULL DEFAULT 0,
    level         INTEGER NOT NULL DEFAULT 1,
    streak_days   INTEGER NOT NULL DEFAULT 0,
    last_active_day TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    -- lang is added by db.init_db() (idempotent ALTER)
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);

-- Passkey (WebAuthn) scaffolding. Populated only when PASSKEYS_ENABLED=true.
CREATE TABLE IF NOT EXISTS passkey_credentials (
    id         TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    public_key TEXT NOT NULL,
    sign_count INTEGER NOT NULL DEFAULT 0,
    transports TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS passkey_challenges (
    challenge  TEXT PRIMARY KEY,
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,
    label         TEXT NOT NULL DEFAULT '',
    encrypted_key TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at  TEXT
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id          INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    default_provider TEXT NOT NULL DEFAULT 'openrouter',
    default_model    TEXT NOT NULL DEFAULT 'mistralai/mistral-large-latest',
    active_key_id    INTEGER REFERENCES api_keys(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    title        TEXT NOT NULL,
    original_ref TEXT,
    raw_text     TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chunks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,
    text      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_id  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    lang       TEXT NOT NULL DEFAULT 'en',
    status     TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    deck_id     INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'queued',
    total       INTEGER NOT NULL DEFAULT 0,
    done        INTEGER NOT NULL DEFAULT 0,
    message     TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    started_at  TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS questions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    deck_id        INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    chunk_id       INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
    prompt         TEXT NOT NULL,
    options_json   TEXT NOT NULL,
    correct_index  INTEGER NOT NULL,
    explanation    TEXT NOT NULL DEFAULT '',
    difficulty     TEXT NOT NULL DEFAULT 'medium',
    model          TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    verify_score   REAL,
    due_at         TEXT NOT NULL DEFAULT (datetime('now')),
    interval_days  REAL NOT NULL DEFAULT 0,
    ef             REAL NOT NULL DEFAULT 2.5,
    reps           INTEGER NOT NULL DEFAULT 0,
    lapses         INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id  INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    rating       TEXT NOT NULL,
    correct      INTEGER NOT NULL,
    xp_awarded   INTEGER NOT NULL DEFAULT 0,
    interval_days REAL NOT NULL DEFAULT 0,
    reviewed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS study_sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    deck_id        INTEGER REFERENCES decks(id) ON DELETE SET NULL,
    started_at     TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at       TEXT,
    cards_reviewed INTEGER NOT NULL DEFAULT 0,
    xp_earned      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS user_badges (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    badge      TEXT NOT NULL,
    awarded_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, badge)
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_jobs_deck ON jobs(deck_id);
CREATE INDEX IF NOT EXISTS idx_questions_deck ON questions(deck_id);
CREATE INDEX IF NOT EXISTS idx_questions_due ON questions(deck_id, due_at);
CREATE INDEX IF NOT EXISTS idx_reviews_question ON reviews(question_id);
CREATE INDEX IF NOT EXISTS idx_reviews_user ON reviews(user_id);
-- NOTE: onboarded_at is added by db.init_db() (idempotent ALTER), not here,
-- so re-running this script on an existing DB never fails.
-- NOTE: applied idempotently by db.init_db()
