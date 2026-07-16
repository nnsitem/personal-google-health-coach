"""SQLite schema and helpers. Multi-user, single-file DB.

V2: every data table is scoped by user_id (LINE userId). The `users` table
holds per-user configuration (Google token, Gemini key, preferences).
"""

import json
import logging
import sqlite3
from contextlib import contextmanager

from coach.config import DB_PATH

log = logging.getLogger(__name__)

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
    hour       INTEGER NOT NULL DEFAULT -1,  -- -1 = daily value; NULL is forbidden
                                             -- because SQLite treats NULLs as
                                             -- DISTINCT in a PK, which breaks upserts
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


# Tables whose PRIMARY KEY changed in v2 to include user_id. SQLite can't ALTER
# a primary key, so a v1 DB needs these tables rebuilt. (col list = new column order)
_PK_REBUILD = {
    "metrics": {
        "create": """
            CREATE TABLE metrics (
                user_id    TEXT NOT NULL DEFAULT '',
                day        TEXT NOT NULL,
                hour       INTEGER,
                data_type  TEXT NOT NULL,
                value_json TEXT NOT NULL,
                source     TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, day, hour, data_type, source)
            )
        """,
        "cols": "user_id, day, hour, data_type, value_json, source, updated_at",
    },
    "sleep_sessions": {
        "create": """
            CREATE TABLE sleep_sessions (
                user_id     TEXT NOT NULL DEFAULT '',
                start       TEXT NOT NULL,
                end         TEXT NOT NULL,
                stages_json TEXT,
                efficiency  REAL,
                score       REAL,
                PRIMARY KEY (user_id, start, end)
            )
        """,
        "cols": "user_id, start, end, stages_json, efficiency, score",
    },
    "exercise_sessions": {
        "create": """
            CREATE TABLE exercise_sessions (
                user_id       TEXT NOT NULL DEFAULT '',
                start         TEXT NOT NULL,
                end           TEXT NOT NULL,
                activity_type TEXT,
                stats_json    TEXT,
                PRIMARY KEY (user_id, start, end)
            )
        """,
        "cols": "user_id, start, end, activity_type, stats_json",
    },
    "goals": {
        "create": """
            CREATE TABLE goals (
                user_id    TEXT NOT NULL DEFAULT '',
                key        TEXT NOT NULL,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, key)
            )
        """,
        "cols": "user_id, key, value_json, updated_at",
    },
    "coach_memory": {
        "create": """
            CREATE TABLE coach_memory (
                user_id    TEXT NOT NULL DEFAULT '',
                name       TEXT NOT NULL,
                content    TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, name)
            )
        """,
        "cols": "user_id, name, content, updated_at",
    },
}


def _user_id_in_pk(conn, table: str) -> bool:
    """Check whether user_id is part of the table's PRIMARY KEY."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    for r in rows:
        # r["pk"] > 0 means the column is part of the primary key
        if r["name"] == "user_id" and r["pk"] > 0:
            return True
    return False


def _rebuild_pk_tables(conn) -> None:
    """Rebuild v1 tables whose PK didn't include user_id (SQLite can't ALTER PK).

    Preserves all rows; v1 rows carry user_id='' at this point and are
    attributed to the v1 user afterwards by _adopt_orphan_rows().
    """
    for table, spec in _PK_REBUILD.items():
        # Skip if table doesn't exist yet (fresh install already has correct PK)
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        if _user_id_in_pk(conn, table):
            continue  # already migrated

        log.info("rebuilding %s to add user_id to primary key", table)

        cols = spec["cols"]
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        conn.executescript(spec["create"])
        conn.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {table}_old")
        conn.execute(f"DROP TABLE {table}_old")


_USER_TABLES = [
    "metrics", "sleep_sessions", "exercise_sessions", "insights",
    "goals", "chat_messages", "coach_memory", "sync_log",
]


def _metrics_hour_nullable(conn) -> bool:
    for r in conn.execute("PRAGMA table_info(metrics)"):
        if r["name"] == "hour":
            return not r["notnull"]
    return False


def _rebuild_metrics_dedupe(conn) -> None:
    """Fix the NULL-hour primary-key hole and drop the stale duplicates it left.

    `hour` was a nullable PK column, and SQLite treats every NULL as DISTINCT
    inside a primary key — so for daily rows (hour=NULL) the upsert's
    ON CONFLICT never fired and each sync INSERTED a fresh row instead of
    updating. Readers that grabbed the first row then served hours-old values
    (e.g. a nudge quoting the just-after-midnight step count at night).
    Rebuild with hour NOT NULL DEFAULT -1, keeping only the newest row per
    logical key.
    """
    if not _metrics_hour_nullable(conn):
        return
    before = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
    log.info("rebuilding metrics: hour NULL -> -1 sentinel + dedupe (%d rows)", before)
    conn.execute("ALTER TABLE metrics RENAME TO metrics_old")
    conn.executescript("""
        CREATE TABLE metrics (
            user_id    TEXT NOT NULL DEFAULT '',
            day        TEXT NOT NULL,
            hour       INTEGER NOT NULL DEFAULT -1,
            data_type  TEXT NOT NULL,
            value_json TEXT NOT NULL,
            source     TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, day, hour, data_type, source)
        )
    """)
    # SQLite guarantees bare columns accompany the MAX() row in a GROUP BY,
    # so value_json comes from the newest row of each group.
    conn.execute("""
        INSERT INTO metrics (user_id, day, hour, data_type, value_json, source, updated_at)
        SELECT user_id, day, COALESCE(hour, -1), data_type, value_json, source, MAX(updated_at)
        FROM metrics_old
        GROUP BY user_id, day, COALESCE(hour, -1), data_type, source
    """)
    conn.execute("DROP TABLE metrics_old")
    after = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
    log.info("metrics rebuilt: %d -> %d rows (%d stale duplicates removed)",
             before, after, before - after)


def _adopt_orphan_rows(conn) -> None:
    """Assign pre-v2 rows (user_id = '') to the sole user, if unambiguous.

    v1 was single-user by construction, so when exactly one user exists the
    orphans must be theirs. UPDATE OR IGNORE skips rows the post-migration
    re-sync already recreated under the real user_id (those are fresher);
    the skipped duplicates are then dropped.
    """
    orphaned = [
        t for t in _USER_TABLES
        if conn.execute(f"SELECT 1 FROM {t} WHERE user_id = '' LIMIT 1").fetchone()
    ]
    if not orphaned:
        return
    users = conn.execute("SELECT line_user_id FROM users").fetchall()
    if len(users) != 1:
        log.warning(
            "orphan user_id='' rows in %s but %d users exist — cannot attribute, leaving as-is",
            orphaned, len(users),
        )
        return
    uid = users[0]["line_user_id"]
    for table in orphaned:
        conn.execute(f"UPDATE OR IGNORE {table} SET user_id = ? WHERE user_id = ''", (uid,))
        dropped = conn.execute(f"DELETE FROM {table} WHERE user_id = ''").rowcount
        log.info("adopted orphan v1 rows in %s for %s (%d duplicates dropped)", table, uid, dropped)


def _encrypt_legacy_credentials(conn) -> None:
    """One-time: encrypt credential columns stored before ENCRYPTION_KEY was set."""
    from coach import crypto
    if not crypto.is_enabled():
        return
    rows = conn.execute(
        "SELECT line_user_id, google_token_json, gemini_api_key FROM users"
    ).fetchall()
    for row in rows:
        updates = {}
        for col in ("google_token_json", "gemini_api_key"):
            val = row[col]
            if not val or crypto.is_encrypted(val):
                continue
            if val.startswith("gAAAAA"):
                # Ciphertext from a DIFFERENT key — re-encrypting would bury it
                # one layer deeper. Leave it; decrypt() already warns loudly.
                log.warning("users.%s for %s is undecryptable ciphertext — skipping",
                            col, row["line_user_id"])
                continue
            updates[col] = crypto.encrypt(val)
        if updates:
            sets = ", ".join(f"{c} = ?" for c in updates)
            conn.execute(
                f"UPDATE users SET {sets} WHERE line_user_id = ?",
                (*updates.values(), row["line_user_id"]),
            )
            log.info("encrypted legacy plaintext %s for user %s",
                     sorted(updates), row["line_user_id"])


def init_db(force: bool = False) -> None:
    """Create schema + run migration + create indexes. Idempotent."""
    global _initialized
    if _initialized and not force:
        return
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        # Migration step 1: add user_id columns to pre-v2 tables that lack them
        for table, col_def in _ADD_COLUMNS:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass  # column already exists
        # Migration step 2: rebuild tables whose PRIMARY KEY must include user_id
        _rebuild_pk_tables(conn)
        # Migration step 2b: fix the NULL-hour PK hole in metrics + dedupe
        _rebuild_metrics_dedupe(conn)
        # Migration step 3: attribute pre-v2 rows (user_id='') to the v1 user
        _adopt_orphan_rows(conn)
        # Migration step 4: encrypt credentials stored before ENCRYPTION_KEY existed
        _encrypt_legacy_credentials(conn)
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


def user_tz(user: dict | None):
    """The user's ZoneInfo, falling back to the server TZ on missing/bad values."""
    from zoneinfo import ZoneInfo
    from coach.config import TZ
    if user and user.get("timezone"):
        try:
            return ZoneInfo(user["timezone"])
        except Exception:
            log.warning("invalid timezone %r for user %s — using server TZ",
                        user["timezone"], user.get("line_user_id"))
    return TZ


def get_user_language(user_id: str) -> str:
    """The user's preferred language as a display name for prompting Gemini
    (e.g. 'Thai', 'English').

    Resolution order: coach_memory 'language' entry (what the user told the
    coach in conversation) → users.language column → 'English'.

    System-generated messages (nudges, daily summary, weekly report) have no
    inbound user text for the model to mirror, so they MUST inject this value
    explicitly or they default to English.
    """
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT content FROM coach_memory WHERE user_id = ? AND lower(name) = 'language'",
                (user_id,),
            ).fetchone()
        if row and row["content"]:
            return row["content"].strip()
    except Exception:
        pass
    user = get_user(user_id)
    if user and user.get("language"):
        return user["language"]
    return "English"


def insight_sent_today(user_id: str, kind: str, tz) -> bool:
    """Whether an insight of this kind exists since the user's local midnight.

    insights.ts is stored as SQLite datetime('now') (UTC), so the local
    midnight is converted to a UTC string for comparison.
    """
    from datetime import datetime, timezone as _timezone
    local_midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = local_midnight.astimezone(_timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM insights WHERE user_id = ? AND kind = ? AND ts >= ? LIMIT 1",
            (user_id, kind, cutoff),
        ).fetchone()
    return row is not None


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
    # hour=None means a daily value; stored as -1 because a NULL inside the
    # primary key would never trigger ON CONFLICT (NULLs are distinct in
    # SQLite PKs) and every sync would insert a duplicate row.
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO metrics (user_id, day, hour, data_type, value_json, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(user_id, day, hour, data_type, source)
            DO UPDATE SET value_json = excluded.value_json, updated_at = datetime('now')
            """,
            (user_id, day, -1 if hour is None else hour, data_type, json.dumps(value), source),
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
