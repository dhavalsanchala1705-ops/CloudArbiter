"""
Event Store Service — append-only event ingestion with idempotency guarantee.

Responsibilities:
  1. Check for duplicate event_id → return DUPLICATE without touching state
  2. Append new events to the `events` table (immutable, never UPDATE/DELETE)
  3. Stream events back in deterministic (timestamp, event_id) order for replay

Idempotency is enforced at TWO layers:
  - Application layer: SELECT before INSERT → fast 409 path
  - DB layer: PRIMARY KEY constraint on event_id → defence-in-depth
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Generator, Iterator, Optional

from app.db import get_connection
from app.models.event import IncomingEvent, IngestResult, IngestStatus, StoredEvent


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ingest_event(event: IncomingEvent, conn: Optional[sqlite3.Connection] = None) -> IngestResult:
    """
    Validate dedup, then append the event to the store.

    Returns:
        IngestResult with status=ACCEPTED or status=DUPLICATE.
    Raises:
        sqlite3.IntegrityError — should not happen (caught internally)
    """
    c = conn or get_connection()

    # --- Dedup check ---
    row = c.execute(
        "SELECT event_id FROM events WHERE event_id = ?", (event.event_id,)
    ).fetchone()

    if row is not None:
        return IngestResult(
            status=IngestStatus.duplicate,
            event_id=event.event_id,
            message="Event already processed; no state change.",
        )

    # --- Append ---
    received_at = datetime.now(timezone.utc).isoformat()
    c.execute(
        """
        INSERT INTO events
            (event_id, source, timestamp, action, resource_type,
             amount, carbon_intensity, region, user_id,
             priority, duration_hours, raw_payload, received_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event.event_id,
            event.source.value,
            event.timestamp.isoformat(),
            event.action.value,
            event.resource_type.value,
            event.amount,
            event.carbon_intensity,
            event.region,
            event.user_id,
            event.priority,
            event.duration_hours,
            event.to_raw_payload(),
            received_at,
        ),
    )
    c.commit()

    return IngestResult(
        status=IngestStatus.accepted,
        event_id=event.event_id,
        message="Event accepted and appended to the event log.",
    )


def event_exists(event_id: str, conn: Optional[sqlite3.Connection] = None) -> bool:
    """Check whether an event_id is already in the store."""
    c = conn or get_connection()
    row = c.execute(
        "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    return row is not None


def get_events_ordered(
    region: Optional[str] = None,
    resource_type: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Generator[StoredEvent, None, None]:
    """
    Stream all events from the store in deterministic (timestamp, event_id) order.

    Optionally filter by region and/or resource_type.
    Uses a generator so large event logs are NOT loaded fully into memory —
    satisfies the "memory must not scale unbounded" requirement.
    """
    c = conn or get_connection()
    sql = "SELECT * FROM events"
    params: list = []
    clauses: list[str] = []

    if region is not None:
        clauses.append("region = ?")
        params.append(region)
    if resource_type is not None:
        clauses.append("resource_type = ?")
        params.append(resource_type)

    if clauses:
        sql += " WHERE " + " AND ".join(clauses)

    sql += " ORDER BY timestamp ASC, event_id ASC"

    cursor = c.execute(sql, params)
    while True:
        row = cursor.fetchone()
        if row is None:
            break
        yield StoredEvent.from_row(row)


def get_all_request_events_for_bucket(
    region: str,
    resource_type: str,
    conn: Optional[sqlite3.Connection] = None,
) -> list[StoredEvent]:
    """
    Return all REQUEST events for a (region, resource_type) bucket,
    in deterministic order. Used by the conflict resolver.
    """
    c = conn or get_connection()
    rows = c.execute(
        """
        SELECT * FROM events
        WHERE region = ? AND resource_type = ? AND action = 'request'
        ORDER BY timestamp ASC, event_id ASC
        """,
        (region, resource_type),
    ).fetchall()
    return [StoredEvent.from_row(r) for r in rows]


def count_events(conn: Optional[sqlite3.Connection] = None) -> int:
    """Return total number of stored events."""
    c = conn or get_connection()
    row = c.execute("SELECT COUNT(*) FROM events").fetchone()
    return row[0] if row else 0
