"""SQLite schema and helpers. Single-user, single-file DB."""

import json
import sqlite3
from contextlib import contextmanager

from coach.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    day        TEXT NOT NULL,             -- YYYY-MM-DD (local civil day)
    hour       INTEGER,                   -- NULL for daily aggregates
    data_type  TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (day, hour, data_type, source)
);

CREATE TABLE IF NOT EXISTS sleep_sessions (
    start       TEXT NOT NULL,
    end         TEXT NOT NULL,
    stages_json TEXT,
    efficiency  REAL,
    score       REAL,
    PRIMARY KEY (start, end)
);

CREATE TABLE IF NOT EXISTS exercise_sessions (
    start         TEXT NOT NULL,
    end           TEXT NOT NULL,
    activity_type TEXT,
    stats_json    TEXT,
    PRIMARY KEY (start, end)
);

CREATE TABLE IF NOT EXISTS insights (
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    content   TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS goals (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_messages (
    ts   TEXT NOT NULL,
    role TEXT NOT NULL,                   -- 'user' | 'coach'
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coach_memory (
    name       TEXT PRIMARY KEY,
    content    TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_log (
    ts        TEXT NOT NULL,
    data_type TEXT NOT NULL,
    ok        INTEGER NOT NULL,
    detail    TEXT
);
"""


@contextmanager
def connect():
    # timeout + busy_timeout: wait up to 30s if another writer holds the lock
    # (scheduler threads + webhook background tasks can write concurrently).
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Schema creation only needs to run once per process (CREATE TABLE IF NOT EXISTS
# on every message/job is wasted work). WAL is a persistent DB property set once.
_initialized = False

# Indexes for the tables that grow over time and are queried by ts / kind.
_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_chat_messages_ts ON chat_messages(ts);
CREATE INDEX IF NOT EXISTS idx_insights_kind_ts ON insights(kind, ts);
CREATE INDEX IF NOT EXISTS idx_sync_log_ts ON sync_log(ts);
"""


def init_db(force: bool = False) -> None:
    global _initialized
    if _initialized and not force:
        return
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")  # persistent; set once is enough
        conn.executescript(SCHEMA)
        conn.executescript(_INDEXES)
    _initialized = True


def upsert_metric(day: str, hour: int | None, data_type: str, value, source: str = "") -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO metrics (day, hour, data_type, value_json, source, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(day, hour, data_type, source)
            DO UPDATE SET value_json = excluded.value_json, updated_at = datetime('now')
            """,
            (day, hour, data_type, json.dumps(value), source),
        )


def upsert_sleep_session(start: str, end: str, stages, efficiency=None, score=None) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO sleep_sessions (start, end, stages_json, efficiency, score)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(start, end)
            DO UPDATE SET stages_json = excluded.stages_json,
                          efficiency = excluded.efficiency,
                          score = excluded.score
            """,
            (start, end, json.dumps(stages), efficiency, score),
        )


def log_sync(data_type: str, ok: bool, detail: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO sync_log (ts, data_type, ok, detail) VALUES (datetime('now'), ?, ?, ?)",
            (data_type, int(ok), detail[:2000]),
        )
