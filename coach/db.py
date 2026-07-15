"""SQLite schema and helpers. Multi-user, single-file DB.

V2: every data table is scoped by user_id (LINE userId). The `users` table
holds per-user configuration (Google token, Gemini key, preferences).
"""

import json
import sqlite3
from contextlib import contextmanager

from coach.config import DB_PATH

# --- Schema (fresh install) -------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    line_user_id     TEXT PRIMARY KEY,
    display_name     TEXT,
    google_token_json TEXT,               -- encrypted JSON blob
    gemini_api_key   TEXT,                -- encrypted
    timezone         TEXT NOT NULL DEFAULT 'Asia/Bangkok',
    language         TEXT NOT NULL DEFAULT 'English',
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    active           INTEGER NOT NULL DEFAULT 1,
    onboarding_state TEXT NOT NULL DEFAULT ''  -- '' | 'awaiting_gemini_key'
);

CREATE TABLE IF NOT EXISTS metrics (
    user_id    TEXT NOT NULL DEFAULT '',
    day        TEXT NOT NULL,
    hour       INTEGER,
    data_type  TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, day, hour, data_type, source)
);

CREATE TABLE IF NOT EXISTS sleep_sessions (
    user_id     TEXT NOT NULL DEFAULT '',
    start       TEXT NOT NULL,
    end         TEXT NOT NULL,
    stages_json TEXT,
    efficiency  REAL,
    score       REAL,
    PRIMARY KEY (user_id, start, end)
);

CREATE TABLE IF NOT EXISTS exercise_sessions (
    user_id       TEXT NOT NULL DEFAULT '',
    start         TEXT NOT NULL,
    end           TEXT NOT NULL,
    activity_type TEXT,
    stats_json    TEXT,
    PRIMARY KEY (user_id, start, end)
);

CREATE TABLE IF NOT EXISTS insights (
    user_id   TEXT NOT NULL DEFAULT '',
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    content   TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS goals (
    user_id    TEXT NOT NULL DEFAULT '',
    key        TEXT NOT NULL,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    user_id TEXT NOT NULL DEFAULT '',
    ts      TEXT NOT NULL,
    role    TEXT NOT NULL,
    text    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coach_memory (
    user_id    TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL,
    content    TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, name)
);

CREATE TABLE IF NOT EXISTS sync_log (
    user_id   TEXT NOT NULL DEFAULT '',
    ts        TEXT NOT NULL,
    data_type TEXT NOT NULL,
    ok        INTEGER NOT NULL,
    detail    TEXT
);
"""

# --- Migration (v1 → v2): add user_id + users table to existing DBs --------

_MIGRATION = """
-- Add users table if missing
CREATE TABLE IF NOT EXISTS users (
    line_user_id     TEXT PRIMARY KEY,
    display_name     TEXT,
    google_token_json TEXT,
    gemini_api_key   TEXT,
    timezone         TEXT NOT NULL DEFAULT 'Asia/Bangkok',
    language         TEXT NOT NULL DEFAULT 'English',
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    active           INTEGER NOT NULL DEFAULT 1,
    onboarding_state TEXT NOT NULL DEFAULT ''
);

-- Add user_id columns where missing (safe: ALTER TABLE ADD COLUMN IF NOT EXISTS
-- isn't supported in SQLite, so we catch errors silently via INSERT OR IGNORE trick).
"""

# Columns to add to existing tables (table_name, column_def).
_ADD_COLUMNS = [
    ("metrics", "user_id TEXT NOT NULL DEFAULT ''"),
    ("sleep_sessions", "user_id TEXT NOT NULL DEFAULT ''"),
    ("exercise_sessions", "user_id TEXT NOT NULL DEFAULT ''"),
    ("insights", "user_id TEXT NOT NULL DEFAULT ''"),
    ("goals", "user_id TEXT NOT NULL DEFAULT ''"),
    ("chat_messages", "user_id TEXT NOT NULL DEFAULT ''"),
    ("coach_memory", "user_id TEXT NOT NULL DEFAULT ''"),
    ("sync_log", "user_id TEXT NOT NULL DEFAULT ''"),
]

# --- Indexes ----------------------------------------------------------------

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_metrics_user_day ON metrics(user_id, day);
CREATE INDEX IF NOT EXISTS idx_chat_messages_user_ts ON chat_messages(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_insights_user_kind_ts ON insights(user_id, kind, ts);
CREATE INDEX IF NOT EXISTS idx_sync_log_user_ts ON sync_log(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_sleep_user_start ON sleep_sessions(user_id, start);
"""

# --- Connection & init ------------------------------------------------------

@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


_initialized = False


def init_db(force: bool = False) -> None:
    """Create schema + run migration + create indexes. Idempotent."""
    global _initialized
    if _initialized and not force:
        return
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        # Migration: add user_id columns to pre-v2 tables that lack them
        for table, col_def in _ADD_COLUMNS:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.executescript(_INDEXES)
    _initialized = True


# --- User management --------------------------------------------------------

def get_user(line_user_id: str) -> dict | None:
    """Look up a user by LINE userId. Returns dict or None.

    Sensitive fields (google_token_json, gemini_api_key) are decrypted on read.
    """
    from coach.crypto import decrypt
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE line_user_id = ?", (line_user_id,)
        ).fetchone()
    if not row:
        return None
    user = dict(row)
    # Decrypt sensitive fields
    if user.get("google_token_json"):
        user["google_token_json"] = decrypt(user["google_token_json"])
    if user.get("gemini_api_key"):
        user["gemini_api_key"] = decrypt(user["gemini_api_key"])
    return user


def create_user(line_user_id: str, display_name: str = "") -> dict:
    """Create a new user record. Returns the user dict."""
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO users (line_user_id, display_name)
            VALUES (?, ?)
            """,
            (line_user_id, display_name),
        )
    return get_user(line_user_id)


def get_or_create_user(line_user_id: str, display_name: str = "") -> dict:
    """Get existing user or create a new one."""
    user = get_user(line_user_id)
    if user is None:
        user = create_user(line_user_id, display_name)
    return user


def update_user(line_user_id: str, **fields) -> None:
    """Update arbitrary fields on a user record.

    Sensitive fields (google_token_json, gemini_api_key) are encrypted before storage.
    """
    if not fields:
        return
    from coach.crypto import encrypt
    # Encrypt sensitive fields before writing
    for key in ("google_token_json", "gemini_api_key"):
        if key in fields and fields[key]:
            fields[key] = encrypt(fields[key])
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [line_user_id]
    with connect() as conn:
        conn.execute(f"UPDATE users SET {sets} WHERE line_user_id = ?", values)


def list_active_users() -> list[dict]:
    """List all active users who have a Google token.

    Sensitive fields are decrypted.
    """
    from coach.crypto import decrypt
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE active = 1 AND google_token_json IS NOT NULL AND google_token_json != ''"
        ).fetchall()
    users = []
    for r in rows:
        user = dict(r)
        if user.get("google_token_json"):
            user["google_token_json"] = decrypt(user["google_token_json"])
        if user.get("gemini_api_key"):
            user["gemini_api_key"] = decrypt(user["gemini_api_key"])
        users.append(user)
    return users


# --- Data helpers (all scoped by user_id) -----------------------------------

def upsert_metric(user_id: str, day: str, hour: int | None, data_type: str, value, source: str = "") -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO metrics (user_id, day, hour, data_type, value_json, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(user_id, day, hour, data_type, source)
            DO UPDATE SET value_json = excluded.value_json, updated_at = datetime('now')
            """,
            (user_id, day, hour, data_type, json.dumps(value), source),
        )


def upsert_sleep_session(user_id: str, start: str, end: str, stages, efficiency=None, score=None) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO sleep_sessions (user_id, start, end, stages_json, efficiency, score)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, start, end)
            DO UPDATE SET stages_json = excluded.stages_json,
                          efficiency = excluded.efficiency,
                          score = excluded.score
            """,
            (user_id, start, end, json.dumps(stages), efficiency, score),
        )


def log_sync(user_id: str, data_type: str, ok: bool, detail: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO sync_log (user_id, ts, data_type, ok, detail) VALUES (?, datetime('now'), ?, ?, ?)",
            (user_id, data_type, int(ok), detail[:2000]),
        )
