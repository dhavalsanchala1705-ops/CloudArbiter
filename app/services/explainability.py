"""
Explainability service — builds human-readable, stepwise explanations
for audit decisions without altering resolver logic or DB schema.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime

from app.services import audit_engine
from app.db import get_connection
from app.models.event import StoredEvent


def explain_decision(decision_id: str) -> Optional[Dict[str, Any]]:
    entry = audit_engine.get_audit_entry(decision_id)
    if entry is None:
        return None

    conn = get_connection()
    ids = entry.event_ids_considered or []
    events: List[StoredEvent] = []
    if ids:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(f"SELECT * FROM events WHERE event_id IN ({placeholders})", ids).fetchall()
        events_map = {r["event_id"]: StoredEvent.from_row(r) for r in rows}
        # preserve original ordering as recorded in audit entry
        for eid in ids:
            if eid in events_map:
                events.append(events_map[eid])

    # determine winner from final_state (audit is authoritative)
    accepted = entry.final_state.get("accepted", []) if entry.final_state else []
    winner_event_id = accepted[0] if accepted else None

    # Prepare checks
    checks: List[Dict[str, Any]] = []

    # Helper to format event id short
    def short(eid: str) -> str:
        return eid[:8]

    # Priority Check
    if not events or entry.conflict_type.value != "overlap_same_region_resource":
        checks.append({"step": "Priority Check", "result": "Skipped", "winner": "Skipped"})
        checks.append({"step": "Carbon Efficiency Check", "result": "Skipped", "winner": "Skipped"})
        checks.append({"step": "Earliest Start Time", "result": "Skipped", "winner": "Skipped"})
        final_reason = entry.resolution_reason
        return {
            "decision_id": entry.decision_id,
            "winner_event_id": winner_event_id,
            "checks": checks,
            "final_reason": final_reason,
        }

    # compute top priority
    priorities = [e.priority for e in events]
    top = max(priorities) if priorities else None
    top_ids = [e.event_id for e in events if e.priority == top]

    if len(top_ids) == 1:
        # priority decided
        # find second best for result string
        second = max((p for p in priorities if p != top), default=None)
        result = f"{top} > {second}" if second is not None else f"{top}"
        checks.append({"step": "Priority Check", "result": result, "winner": top_ids[0]})
        # subsequent checks skipped
        checks.append({"step": "Carbon Efficiency Check", "result": "Skipped", "winner": "Skipped"})
        checks.append({"step": "Earliest Start Time", "result": "Skipped", "winner": "Skipped"})
    else:
        # tie on priority — evaluate carbon
        checks.append({"step": "Priority Check", "result": "Tie", "winner": "Skipped"})

        # Carbon Efficiency Check
        tied_events = [e for e in events if e.event_id in top_ids]
        carbon_vals = [e.carbon_intensity for e in tied_events]
        min_c = min(carbon_vals) if carbon_vals else None
        min_ids = [e.event_id for e in tied_events if e.carbon_intensity == min_c]
        if len(min_ids) == 1:
            # carbon decided
            # find second best carbon for result
            second = min((c for c in carbon_vals if c != min_c), default=None)
            result = f"{min_c} < {second}" if second is not None else f"{min_c}"
            checks.append({"step": "Carbon Efficiency Check", "result": result, "winner": min_ids[0]})
            checks.append({"step": "Earliest Start Time", "result": "Skipped", "winner": "Skipped"})
        else:
            # tie continues — earliest start time
            checks.append({"step": "Carbon Efficiency Check", "result": "Tie", "winner": "Skipped"})
            tied_by_carbon = [e for e in tied_events if e.event_id in min_ids]
            # sort by timestamp
            tied_sorted = sorted(tied_by_carbon, key=lambda e: e.timestamp)
            if len(tied_sorted) >= 1:
                winner = tied_sorted[0]
                # if more than one with identical timestamp, we still pick first (deterministic)
                checks.append({"step": "Earliest Start Time", "result": f"{tied_sorted[0].timestamp.isoformat()}", "winner": winner.event_id})
            else:
                checks.append({"step": "Earliest Start Time", "result": "Skipped", "winner": "Skipped"})

    final_reason = entry.resolution_reason

    return {
        "decision_id": entry.decision_id,
        "winner_event_id": winner_event_id,
        "checks": checks,
        "final_reason": final_reason,
    }
