"""
SQLite connection management and schema initialisation.

Design decisions:
  - WAL journal mode → better read/write concurrency (important for FastAPI async handlers)
  - PRAGMA foreign_keys = ON
  - All three tables created here:
      • events      — append-only, immutable event log  (PRIMARY KEY = dedup key)
      • state_snapshot — derived projection, safe to truncate & rebuild
      • audit_log   — append-only, immutable decision log
  - get_connection() returns a module-level connection; for tests, a
    separate in-memory connection is injected via dependency override.
"""
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from app.config import DB_PATH

_local = threading.local()

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------
_CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT    PRIMARY KEY,
    source          TEXT    NOT NULL,
    timestamp       TEXT    NOT NULL,
    action          TEXT    NOT NULL,
    resource_type   TEXT    NOT NULL,
    amount          REAL    NOT NULL,
    carbon_intensity REAL   NOT NULL,
    region          TEXT    NOT NULL,
    user_id         TEXT    NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 5,
    duration_hours  REAL    NOT NULL DEFAULT 1.0,
    raw_payload     TEXT    NOT NULL,
    received_at     TEXT    NOT NULL
);
"""

_CREATE_STATE_SNAPSHOT = """
CREATE TABLE IF NOT EXISTS state_snapshot (
    region              TEXT    NOT NULL,
    resource_type       TEXT    NOT NULL,
    allocated_amount    REAL    NOT NULL DEFAULT 0.0,
    carbon_budget_used  REAL    NOT NULL DEFAULT 0.0,
    version             INTEGER NOT NULL DEFAULT 0,
    last_event_id       TEXT,
    PRIMARY KEY (region, resource_type)
);
"""

_CREATE_AUDIT_LOG = """
CREATE TABLE IF NOT EXISTS audit_log (
    decision_id             TEXT    PRIMARY KEY,
    event_ids_considered    TEXT    NOT NULL,
    conflict_type           TEXT    NOT NULL,
    resolution_reason       TEXT    NOT NULL,
    final_state             TEXT    NOT NULL,
    decision_timestamp      TEXT    NOT NULL
);
"""

_PRAGMAS = [
    "PRAGMA journal_mode=WAL;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA synchronous=NORMAL;",
]

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _make_connection(path: str) -> sqlite3.Connection:
    """Create a new SQLite connection with row_factory and pragmas applied."""
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    return conn


# Module-level singleton (overridden in tests via _override_connection)
_override: Optional[sqlite3.Connection] = None


def get_connection() -> sqlite3.Connection:
    """
    Return the active SQLite connection.
    Tests can call set_test_connection() to inject an in-memory DB.
    """
    if _override is not None:
        return _override
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = _make_connection(DB_PATH)
    return _local.conn


def set_test_connection(conn: sqlite3.Connection) -> None:
    """Inject a test/in-memory connection (called from conftest.py)."""
    global _override
    _override = conn


def clear_test_connection() -> None:
    """Remove the injected test connection."""
    global _override
    _override = None


def init_db(conn: Optional[sqlite3.Connection] = None) -> None:
    """
    Create all tables if they don't exist.
    Idempotent — safe to call on every startup.
    """
    c = conn or get_connection()
    c.execute(_CREATE_EVENTS)
    c.execute(_CREATE_STATE_SNAPSHOT)
    c.execute(_CREATE_AUDIT_LOG)
    c.commit()
