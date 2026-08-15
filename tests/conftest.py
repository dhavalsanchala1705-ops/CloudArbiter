"""
pytest configuration and shared fixtures.

Every test gets:
  - A fresh in-memory SQLite database (no cross-test pollution)
  - A FastAPI TestClient wired to that in-memory DB
  - A set of factory helpers to build valid IncomingEvent objects
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

from app.db import clear_test_connection, init_db, set_test_connection
from app.main import app


# ---------------------------------------------------------------------------
# DB fixture — fresh in-memory SQLite per test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_db():
    """
    Creates a new in-memory SQLite database for each test, injects it into
    the db module, then tears it down after the test completes.
    This prevents any state from leaking between tests.
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    init_db(conn)

    set_test_connection(conn)
    yield conn
    clear_test_connection()
    conn.close()


# ---------------------------------------------------------------------------
# HTTP client fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def client(fresh_db) -> TestClient:
    """FastAPI TestClient using the fresh in-memory DB."""
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Event factory helpers
# ---------------------------------------------------------------------------

def make_event(
    event_id: str = "test-event-0001",
    source: str = "ai_training",
    timestamp: Optional[str] = None,
    action: str = "request",
    resource_type: str = "GPU",
    amount: float = 100.0,
    carbon_intensity: float = 120.0,
    region: str = "us-west-2",
    user_id: str = "user-test",
    priority: int = 5,
    duration_hours: float = 2.0,
    **extra: Any,
) -> Dict[str, Any]:
    """
    Return a dict representing a valid IncomingEvent.
    Override any field by passing it as a keyword argument.
    Pass field=None to omit it entirely (for testing missing-field validation).
    """
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    base = {
        "event_id": event_id,
        "source": source,
        "timestamp": ts,
        "action": action,
        "resource_type": resource_type,
        "amount": amount,
        "carbon_intensity": carbon_intensity,
        "region": region,
        "user_id": user_id,
        "priority": priority,
        "duration_hours": duration_hours,
    }
    base.update(extra)
    # Allow callers to remove fields by passing field=None explicitly via **extra
    return {k: v for k, v in base.items() if v is not None}


def make_overlapping_event(
    base_ts: datetime,
    offset_hours: float = 0.5,
    duration_hours: float = 2.0,
    **kwargs,
) -> Dict[str, Any]:
    """
    Create an event whose window overlaps with one starting at base_ts
    (offset by offset_hours, so they are guaranteed to share time).
    """
    ts = (base_ts + timedelta(hours=offset_hours)).isoformat()
    return make_event(timestamp=ts, duration_hours=duration_hours, **kwargs)
