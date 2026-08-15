"""
State Reconstructor — folds the ordered event log into a versioned state snapshot.

This module has two modes:
  1. Full rebuild (rebuild_state_from_events): used by the replay script.
     Streams all events from the DB in (timestamp, event_id) order,
     truncates the state_snapshot table, then rebuilds it from scratch.
     This is the CORRECTNESS PROOF: if this matches the live state, the
     system is deterministic and replay-consistent.

  2. Incremental update (apply_event_to_state): used by the online path
     (POST /events). Processes a single bucket re-fold after appending
     one new event. Much faster than a full rebuild.

Both modes call the conflict resolver and write audit entries.
Neither mode ever mutates the events table.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Dict, Optional, Set, Tuple

from app.config import CAPACITY
from app.db import get_connection, init_db
from app.models.audit import ConflictType
from app.models.event import StoredEvent
from app.models.state import ResourceBucket, StateDelta, StateSnapshot
from app.services import audit_engine, conflict_resolver, event_store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BucketKey = Tuple[str, str]  # (region, resource_type)


def _get_capacity(resource_type: str) -> float:
    return CAPACITY.get(resource_type, 1000.0)


def _upsert_bucket(
    bucket: ResourceBucket,
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        """
        INSERT INTO state_snapshot
            (region, resource_type, allocated_amount, carbon_budget_used, version, last_event_id)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(region, resource_type) DO UPDATE SET
            allocated_amount   = excluded.allocated_amount,
            carbon_budget_used = excluded.carbon_budget_used,
            version            = excluded.version,
            last_event_id      = excluded.last_event_id
        """,
        (
            bucket.region,
            bucket.resource_type,
            bucket.allocated_amount,
            bucket.carbon_budget_used,
            bucket.version,
            bucket.last_event_id,
        ),
    )


def _read_bucket(
    region: str,
    resource_type: str,
    conn: sqlite3.Connection,
) -> ResourceBucket:
    row = conn.execute(
        "SELECT * FROM state_snapshot WHERE region = ? AND resource_type = ?",
        (region, resource_type),
    ).fetchone()
    if row:
        return ResourceBucket.from_row(row)
    return ResourceBucket(region=region, resource_type=resource_type)


# ---------------------------------------------------------------------------
# Bucket-level fold — core logic shared by both modes
# ---------------------------------------------------------------------------

def _fold_bucket(
    region: str,
    resource_type: str,
    conn: sqlite3.Connection,
    accepted_globally: Optional[Set[str]] = None,
    rejected_globally: Optional[Set[str]] = None,
) -> Tuple[ResourceBucket, Optional[str]]:
    """
    Re-compute the state for a single (region, resource_type) bucket
    by folding all events for that bucket in deterministic order.

    Returns:
        (new_bucket, audit_entry_id_or_None)

    Side-effects:
        May write one audit entry if a conflict is detected.
        Does NOT write to state_snapshot — caller is responsible.

    accepted_globally / rejected_globally:
        Optional shared sets for tracking accepted/rejected event_ids
        across a full-rebuild run (avoids duplicate audit entries).
    """
    if accepted_globally is None:
        accepted_globally = set()
    if rejected_globally is None:
        rejected_globally = set()

    capacity = _get_capacity(resource_type)
    request_events = event_store.get_all_request_events_for_bucket(region, resource_type, conn)

    # --- Conflict resolution over ALL request events in bucket ---
    overlap_groups = conflict_resolver.find_overlapping_groups(request_events)

    new_accepted: Set[str] = set()
    new_rejected: Set[str] = set()
    audit_entry_id: Optional[str] = None

    # Process each overlap group independently
    for group in overlap_groups:
        # Available capacity = total - already committed from previous groups
        already_allocated = sum(
            e.amount for e in request_events if e.event_id in new_accepted
        )
        available = capacity - already_allocated

        result = conflict_resolver.resolve_conflicts(group, available)

        for eid in result.accepted:
            new_accepted.add(eid)
        for eid in result.rejected:
            new_rejected.add(eid)

        # Write audit entry only if there's a real conflict (new rejections)
        newly_rejected = [eid for eid in result.rejected if eid not in rejected_globally]
        if newly_rejected or result.conflict_detected:
            all_ids = [e.event_id for e in group]
            current_bucket = _read_bucket(region, resource_type, conn)
            audit_entry = audit_engine.write_audit_entry(
                event_ids_considered=all_ids,
                conflict_type=ConflictType(result.conflict_type),
                resolution_reason=result.resolution_reason,
                final_state={
                    "region": region,
                    "resource_type": resource_type,
                    "capacity": capacity,
                    "allocated": result.total_allocated,
                    "accepted": result.accepted,
                    "rejected": result.rejected,
                },
                conn=conn,
            )
            audit_entry_id = audit_entry.decision_id

    # --- Handle release events ---
    release_events = list(event_store.get_events_ordered(region, resource_type, conn))
    release_events = [e for e in release_events if e.action.value == "release"]

    total_released: float = 0.0
    for rel in release_events:
        # Orphan release: no matching accepted request for this user/region/type
        is_orphan = conflict_resolver.is_orphan_release(
            rel,
            accepted_event_ids=new_accepted,
            prior_requests=request_events,
        )
        if is_orphan:
            # Write orphan release audit entry
            audit_engine.write_audit_entry(
                event_ids_considered=[rel.event_id],
                conflict_type=ConflictType.orphan_release,
                resolution_reason=(
                    f"Release event {rel.event_id!r} rejected: no matching accepted "
                    f"request found for user_id={rel.user_id!r}, "
                    f"resource_type={rel.resource_type.value!r}, region={rel.region!r}. "
                    f"State unchanged."
                ),
                final_state={
                    "region": region,
                    "resource_type": resource_type,
                    "orphan_release_event_id": rel.event_id,
                },
                conn=conn,
            )
        else:
            total_released += rel.amount

    # --- Compute final allocated amount ---
    total_accepted_amount = sum(
        e.amount for e in request_events if e.event_id in new_accepted
    )
    allocated = max(0.0, total_accepted_amount - total_released)

    # --- Carbon budget used = sum of (amount * carbon_intensity) for accepted requests ---
    carbon_used = sum(
        e.amount * e.carbon_intensity
        for e in request_events
        if e.event_id in new_accepted
    )

    # Find the last applied event (by deterministic order)
    all_events_ordered = list(event_store.get_events_ordered(region, resource_type, conn))
    last_event_id = all_events_ordered[-1].event_id if all_events_ordered else None

    bucket = ResourceBucket(
        region=region,
        resource_type=resource_type,
        allocated_amount=round(allocated, 6),
        carbon_budget_used=round(carbon_used, 6),
        version=len(all_events_ordered),
        last_event_id=last_event_id,
    )

    # Update global tracking sets
    accepted_globally.update(new_accepted)
    rejected_globally.update(new_rejected)

    return bucket, audit_entry_id


# ---------------------------------------------------------------------------
# Online path: apply a single event incrementally
# ---------------------------------------------------------------------------

def apply_event_to_state(
    event: StoredEvent,
    conn: Optional[sqlite3.Connection] = None,
) -> StateDelta:
    """
    Incrementally update state after a new event has been appended to the store.
    Re-folds only the affected (region, resource_type) bucket.
    """
    c = conn or get_connection()

    # Read old bucket state for delta calculation
    old_bucket = _read_bucket(event.region, event.resource_type.value, c)
    previous_amount = old_bucket.allocated_amount

    # Re-fold the bucket
    new_bucket, audit_entry_id = _fold_bucket(
        event.region, event.resource_type.value, c
    )

    # Persist the updated bucket
    _upsert_bucket(new_bucket, c)
    c.commit()

    return StateDelta(
        event_id=event.event_id,
        region=event.region,
        resource_type=event.resource_type.value,
        previous_amount=previous_amount,
        new_amount=new_bucket.allocated_amount,
        conflict_detected=audit_entry_id is not None,
        audit_entry_id=audit_entry_id,
    )


# ---------------------------------------------------------------------------
# Full rebuild: used by replay script and correctness checks
# ---------------------------------------------------------------------------

def rebuild_state_from_events(
    conn: Optional[sqlite3.Connection] = None,
) -> StateSnapshot:
    """
    Discard the state_snapshot table and rebuild it entirely from the
    events table in deterministic (timestamp, event_id) order.

    This is the idempotency/replay-consistency proof:
        rebuild_state_from_events() == live_state  ⟺  system is correct

    Returns:
        Fresh StateSnapshot derived purely from the event log.
    """
    c = conn or get_connection()

    # Discover all distinct (region, resource_type) buckets from events
    rows = c.execute(
        "SELECT DISTINCT region, resource_type FROM events ORDER BY region, resource_type"
    ).fetchall()
    buckets_keys: list[tuple[str, str]] = [(r["region"], r["resource_type"]) for r in rows]

    # Truncate derived projection (safe — event log is the source of truth)
    c.execute("DELETE FROM state_snapshot")
    c.commit()

    # Truncate audit log too for clean rebuild (deterministic audit on replay)
    c.execute("DELETE FROM audit_log")
    c.commit()

    accepted_globally: Set[str] = set()
    rejected_globally: Set[str] = set()
    buckets = []

    for region, resource_type in buckets_keys:
        bucket, _ = _fold_bucket(
            region, resource_type, c,
            accepted_globally=accepted_globally,
            rejected_globally=rejected_globally,
        )
        _upsert_bucket(bucket, c)
        buckets.append(bucket)

    c.commit()

    total_events = event_store.count_events(c)
    return StateSnapshot(buckets=buckets, total_events_applied=total_events)


# ---------------------------------------------------------------------------
# Read current state from snapshot table
# ---------------------------------------------------------------------------

def get_current_state(conn: Optional[sqlite3.Connection] = None) -> StateSnapshot:
    """Return the current state from the state_snapshot projection table."""
    c = conn or get_connection()
    rows = c.execute(
        "SELECT * FROM state_snapshot ORDER BY region, resource_type"
    ).fetchall()
    buckets = [ResourceBucket.from_row(r) for r in rows]
    total = event_store.count_events(c)
    return StateSnapshot(buckets=buckets, total_events_applied=total)


def get_state_at_timestamp(target_dt: datetime) -> StateSnapshot:
    """
    Reconstruct the state snapshot at a specific point in time.
    Fetches events <= target_dt, seeds a fresh in-memory SQLite DB,
    and runs the standard rebuild_state_from_events on it.
    
    This preserves the active database state completely.
    """
    # Force target_dt to UTC timezone-aware for correct comparison
    if target_dt.tzinfo is None:
        target_dt = target_dt.replace(tzinfo=timezone.utc)
    else:
        target_dt = target_dt.astimezone(timezone.utc)

    # 1. Fetch all events from standard store
    all_events = list(event_store.get_events_ordered())
    
    # 2. Filter events up to target_dt
    filtered_events = []
    for e in all_events:
        e_ts = e.timestamp
        if e_ts.tzinfo is None:
            e_ts = e_ts.replace(tzinfo=timezone.utc)
        else:
            e_ts = e_ts.astimezone(timezone.utc)
        if e_ts <= target_dt:
            filtered_events.append(e)

    # 3. Create temporary in-memory connection
    temp_conn = sqlite3.connect(":memory:", check_same_thread=False)
    temp_conn.row_factory = sqlite3.Row
    temp_conn.execute("PRAGMA journal_mode=WAL;")
    temp_conn.execute("PRAGMA foreign_keys=ON;")

    # 4. Initialize schemas
    init_db(temp_conn)

    # 5. Populate in-memory database events table
    for event in filtered_events:
        temp_conn.execute(
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
                event.raw_payload,
                event.received_at.isoformat(),
            ),
        )
    temp_conn.commit()

    # 6. Rebuild state on temp DB
    snapshot = rebuild_state_from_events(temp_conn)
    temp_conn.close()
    return snapshot
