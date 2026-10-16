"""One SQLite file holds all app state. FTS5 gives us a persistent, incremental BM25 index."""
import json
import sqlite3
import threading
from contextlib import contextmanager

from app.config import settings

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY, doc TEXT, strategy TEXT, section TEXT,
    parent_id TEXT, content TEXT, created_at REAL DEFAULT (unixepoch('subsec'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    id UNINDEXED, strategy UNINDEXED, content, tokenize = 'porter unicode61'
);
CREATE TABLE IF NOT EXISTS parents (id TEXT PRIMARY KEY, doc TEXT, content TEXT);

CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY, kind TEXT, input TEXT, output TEXT, status TEXT,
    prompt_version TEXT, model TEXT, started REAL, ended REAL,
    ttft_ms REAL, total_ms REAL, tokens_in INTEGER, tokens_out INTEGER, meta TEXT
);
CREATE TABLE IF NOT EXISTS spans (
    id TEXT PRIMARY KEY, trace_id TEXT, parent_id TEXT, name TEXT,
    started REAL, ended REAL, ms REAL, input TEXT, output TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS spans_trace ON spans(trace_id);

CREATE TABLE IF NOT EXISTS prompts (
    name TEXT, version INTEGER, template TEXT, note TEXT,
    created_at REAL DEFAULT (unixepoch('subsec')), PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS prompt_active (name TEXT PRIMARY KEY, version INTEGER);
CREATE TABLE IF NOT EXISTS prompt_events (
    name TEXT, from_v INTEGER, to_v INTEGER, action TEXT, at REAL DEFAULT (unixepoch('subsec'))
);

CREATE TABLE IF NOT EXISTS cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT, embedding TEXT, answer TEXT,
    sources TEXT, corpus_version TEXT, prompt_version TEXT, hits INTEGER DEFAULT 0,
    created_at REAL DEFAULT (unixepoch('subsec'))
);
CREATE TABLE IF NOT EXISTS cache_events (
    hit INTEGER, similarity REAL, saved_ms REAL, at REAL DEFAULT (unixepoch('subsec'))
);

CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT, role TEXT, content TEXT, at REAL DEFAULT (unixepoch('subsec'))
);
CREATE TABLE IF NOT EXISTS memory_facts (
    id TEXT PRIMARY KEY, session_id TEXT, fact TEXT, kind TEXT,
    created_at REAL DEFAULT (unixepoch('subsec')), superseded_by TEXT
);
CREATE TABLE IF NOT EXISTS session_summaries (session_id TEXT PRIMARY KEY, summary TEXT, upto INTEGER);

CREATE TABLE IF NOT EXISTS eval_runs (
    id TEXT PRIMARY KEY, name TEXT, config TEXT, metrics TEXT, rows TEXT,
    created_at REAL DEFAULT (unixepoch('subsec'))
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


_conn = _connect()
_conn.executescript(SCHEMA)


@contextmanager
def tx():
    with _lock:
        try:
            yield _conn
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


def query(sql: str, params=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


def one(sql: str, params=()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def get_meta(key: str, default=None):
    row = one("SELECT value FROM meta WHERE key=?", (key,))
    return json.loads(row["value"]) if row else default


def set_meta(key: str, value) -> None:
    with tx() as c:
        c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))
