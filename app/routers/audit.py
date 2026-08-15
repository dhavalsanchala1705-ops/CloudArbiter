"""
GET /audit — immutable audit log endpoints.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from app.db import get_connection
from app.models.audit import AuditEntry
from app.models.event import StoredEvent
from app.services import audit_engine

router = APIRouter(prefix="/audit", tags=["Audit"])


@router.get(
    "",
    response_model=List[AuditEntry],
    summary="Get the full audit log (paginated)",
    description=(
        "Returns all conflict resolution decisions in chronological order. "
        "The audit log is append-only and immutable — entries are never updated or deleted. "
        "Use limit/offset for pagination."
    ),
)
async def get_audit_log(
    limit: int = Query(default=50, ge=1, le=500, description="Max entries to return"),
    offset: int = Query(default=0, ge=0, description="Number of entries to skip"),
) -> List[AuditEntry]:
    return audit_engine.get_audit_log(limit=limit, offset=offset)


@router.get(
    "/{decision_id}",
    response_model=AuditEntry,
    summary="Get a single audit decision",
    description="Returns the full details of a single conflict resolution decision by its UUID.",
)
async def get_audit_entry(decision_id: str) -> AuditEntry:
    entry = audit_engine.get_audit_entry(decision_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit entry not found: {decision_id!r}",
        )
    return entry


@router.get(
    "/{decision_id}/visual",
    summary="Get visual data for conflict resolution",
)
async def get_audit_entry_visual(decision_id: str):
    entry = audit_engine.get_audit_entry(decision_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit entry not found: {decision_id!r}",
        )
    
    conn = get_connection()
    ids = entry.event_ids_considered
    requests_details = []
    if ids:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT * FROM events WHERE event_id IN ({placeholders})",
            ids
        ).fetchall()
        events_map = {r["event_id"]: StoredEvent.from_row(r) for r in rows}
        for eid in ids:
            if eid in events_map:
                requests_details.append(events_map[eid])
                
    accepted_ids = entry.final_state.get("accepted", [])
    winner = accepted_ids[0] if accepted_ids else None
    
    reason_detail = ""
    if entry.conflict_type.value == "orphan_release":
        reason_detail = "Release rejected because no active request matched this user/region/resource."
    elif entry.conflict_type.value == "overlap_same_region_resource":
        reason_detail = (
            f"Capacity over-allocated. Rules evaluated in order: "
            f"1. Priority level (higher wins) "
            f"2. Regional carbon intensity (greener region wins tie) "
            f"3. Start time (earlier wins tie). "
            f"Result: {len(accepted_ids)} accepted, {len(entry.final_state.get('rejected', []))} rejected."
        )
    else:
        reason_detail = "No resource conflict detected. Request fits within existing region capacity."

    return {
        "requests": requests_details,
        "winner_event_id": winner,
        "conflict_type": entry.conflict_type.value,
        "resolution_reason": entry.resolution_reason,
        "reason_detail": reason_detail,
    }
