"""
Conflict Resolver — pure deterministic function, no side-effects.

This module contains ONLY the resolution logic. It does NOT write to the DB.
The calling service (state_reconstructor) is responsible for persistence.

Resolution rules (applied in order):
    Rule 1: Higher priority integer wins (descending sort)
    Rule 2: [tie] Lower carbon_intensity wins (ascending sort — greener region)
    Rule 3: [tie] Earlier timestamp wins (ascending sort — first-come-first-served)
    Rule 4: [hard] Total accepted allocation ≤ available capacity (always enforced)
    Rule 5: Orphan releases (no matching prior allocation) are rejected gracefully

Being a pure function of an ordered list makes this trivially testable and
guarantees the same resolution regardless of when/how many times it is called.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

from app.models.event import StoredEvent


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ResolutionResult:
    """
    Output of the resolver — which events are accepted/rejected and why.
    All fields are immutable after construction.
    """
    accepted: List[str] = field(default_factory=list)   # event_ids
    rejected: List[str] = field(default_factory=list)   # event_ids
    conflict_detected: bool = False
    conflict_type: str = "no_conflict"
    resolution_reason: str = ""
    rule_applied: str = ""
    total_allocated: float = 0.0


# ---------------------------------------------------------------------------
# Core pure function
# ---------------------------------------------------------------------------

def resolve_conflicts(
    candidates: List[StoredEvent],
    available_capacity: float,
) -> ResolutionResult:
    """
    Given a list of overlapping REQUEST events competing for the same
    (resource_type, region) bucket and the remaining available capacity,
    return a ResolutionResult indicating which events are accepted/rejected.

    This is a PURE FUNCTION:
      - No DB access
      - No side-effects
      - Deterministic: same input → same output always

    Args:
        candidates:         All request events in this bucket (ordered by
                            the caller in (timestamp, event_id) order from DB).
                            The resolver re-sorts them by priority rules.
        available_capacity: Remaining capacity BEFORE any of these events
                            are applied (in resource-type units).

    Returns:
        ResolutionResult
    """
    if not candidates:
        return ResolutionResult()

    # --- Sort by resolution priority ---
    # Primary:   priority DESC (higher number = more important)
    # Secondary: carbon_intensity ASC (lower = greener, wins tie)
    # Tertiary:  timestamp ASC (earlier = first-come-first-served)
    # Quaternary: event_id ASC (final deterministic tiebreaker)
    sorted_candidates = sorted(
        candidates,
        key=lambda e: (
            -e.priority,            # Rule 1: higher wins → negate for ascending sort
            e.carbon_intensity,     # Rule 2: lower wins
            e.timestamp,            # Rule 3: earlier wins
            e.event_id,             # Rule 4: lexicographic tiebreaker
        ),
    )

    # --- Greedy accept until capacity exhausted (Rule 4 hard cap) ---
    accepted: List[str] = []
    rejected: List[str] = []
    running_total: float = 0.0
    conflict_detected = False

    for event in sorted_candidates:
        if running_total + event.amount <= available_capacity:
            accepted.append(event.event_id)
            running_total += event.amount
        else:
            rejected.append(event.event_id)
            conflict_detected = True

    # --- Build human-readable reason ---
    reason_parts: List[str] = []
    if conflict_detected:
        winner = next(e for e in sorted_candidates if e.event_id in accepted[:1]) if accepted else None
        loser_ids = rejected[:3]  # summarise first 3

        reason_parts.append(
            f"Conflict detected: {len(rejected)} event(s) rejected due to "
            f"insufficient capacity (available={available_capacity:.2f}, "
            f"requested_total={sum(e.amount for e in sorted_candidates):.2f})."
        )
        if winner:
            reason_parts.append(
                f"Winner event_id={winner.event_id!r} "
                f"(priority={winner.priority}, carbon_intensity={winner.carbon_intensity}, "
                f"timestamp={winner.timestamp.isoformat()})."
            )
        reason_parts.append(
            f"Rejected event_id(s): {loser_ids}. "
            f"Rule applied: PRIORITY_DESC → CARBON_ASC → TIMESTAMP_ASC → HARD_CAP."
        )
        rule = "PRIORITY_THEN_CARBON_THEN_TIMESTAMP"
        ctype = "overlap_same_region_resource"
    else:
        reason_parts.append(
            f"No conflict: all {len(accepted)} event(s) accepted within capacity "
            f"(available={available_capacity:.2f}, allocated={running_total:.2f})."
        )
        rule = "NO_CONFLICT"
        ctype = "no_conflict"

    return ResolutionResult(
        accepted=accepted,
        rejected=rejected,
        conflict_detected=conflict_detected,
        conflict_type=ctype,
        resolution_reason=" ".join(reason_parts),
        rule_applied=rule,
        total_allocated=running_total,
    )


# ---------------------------------------------------------------------------
# Overlap detection helper
# ---------------------------------------------------------------------------

def find_overlapping_groups(events: List[StoredEvent]) -> List[List[StoredEvent]]:
    """
    Group request events into clusters where at least one pair overlaps.
    Uses a sweep-line approach: O(n log n).

    Two events overlap if their time windows [start, end) intersect.
    Events in the same group compete for capacity and are passed together
    to resolve_conflicts().
    """
    if not events:
        return []

    # Sort by window start
    sorted_events = sorted(events, key=lambda e: (e.timestamp, e.event_id))
    groups: List[List[StoredEvent]] = []

    current_group: List[StoredEvent] = [sorted_events[0]]
    current_max_end = sorted_events[0].window_end()

    for evt in sorted_events[1:]:
        if evt.window_start() < current_max_end:
            # Overlaps with the current group
            current_group.append(evt)
            current_max_end = max(current_max_end, evt.window_end())
        else:
            # No overlap → flush group, start new one
            groups.append(current_group)
            current_group = [evt]
            current_max_end = evt.window_end()

    groups.append(current_group)
    return groups


# ---------------------------------------------------------------------------
# Orphan release detection
# ---------------------------------------------------------------------------

def is_orphan_release(
    release_event: StoredEvent,
    accepted_event_ids: set[str],
    prior_requests: List[StoredEvent],
) -> bool:
    """
    A release is "orphaned" if there is no accepted request event
    for the same (user_id, resource_type, region) that it could be
    releasing.  Used by the state reconstructor to reject bad releases.
    """
    matching = [
        r for r in prior_requests
        if r.user_id == release_event.user_id
        and r.resource_type == release_event.resource_type
        and r.region == release_event.region
        and r.event_id in accepted_event_ids
    ]
    return len(matching) == 0
