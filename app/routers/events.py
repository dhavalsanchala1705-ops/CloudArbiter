"""
POST /events — event ingestion endpoint.

Processing pipeline per request:
  1. Pydantic validation  → 400 if schema invalid (automatic, never touches DB)
  2. Dedup check          → 409 if event_id already seen
  3. Append to event log  → immutable INSERT
  4. Incremental state update for affected bucket
  5. Return 200 + state delta (+ audit_entry_id if conflict resolved)
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.db import get_connection
from app.models.event import IncomingEvent, IngestStatus
from app.models.state import StateDelta
from app.services import event_store, state_reconstructor

router = APIRouter(prefix="/events", tags=["Events"])


@router.get(
    "/timerange",
    summary="Get timerange of all events",
    description="Returns the min and max timestamp of all ingested events.",
)
async def get_events_timerange():
    conn = get_connection()
    row = conn.execute(
        "SELECT MIN(timestamp) as min_t, MAX(timestamp) as max_t FROM events"
    ).fetchone()
    if row and row["min_t"] is not None:
        return {
            "min_timestamp": row["min_t"],
            "max_timestamp": row["max_t"]
        }
    return {
        "min_timestamp": None,
        "max_timestamp": None
    }


@router.post(
    "",
    status_code=status.HTTP_200_OK,
    response_model=StateDelta,
    summary="Ingest a resource allocation event",
    description=(
        "Accepts a resource allocation event (request or release). "
        "Validates schema, enforces idempotency via event_id, appends to the "
        "immutable event log, and returns the resulting state delta. "
        "Conflicts are resolved deterministically and recorded in the audit log."
    ),
)
async def ingest_event(event: IncomingEvent) -> StateDelta:
    # Step 1: Dedup check + append
    result = event_store.ingest_event(event)

    if result.status == IngestStatus.duplicate:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "duplicate_event",
                "event_id": event.event_id,
                "message": "Event already processed. No state change occurred. "
                           "This is an idempotent no-op — the system is consistent.",
            },
        )

    # Step 2: Read the stored event back (ensures we use DB-canonical representation)
    stored = next(
        e for e in event_store.get_events_ordered(event.region, event.resource_type.value)
        if e.event_id == event.event_id
    )

    # Step 3: Incremental state update
    delta = state_reconstructor.apply_event_to_state(stored)

    return delta
