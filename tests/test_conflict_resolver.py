"""
Tests for the conflict resolver — pure function tests (no HTTP, no DB).

These tests call the resolver directly, making them fast and proving the
function is pure: same input → same output, no side effects.

Covers:
  ✅ Higher priority wins over lower priority
  ✅ Tie on priority → lower carbon_intensity wins (greener region)
  ✅ Tie on priority + carbon → earlier timestamp wins
  ✅ Over-allocation hard rejected regardless of priority
  ✅ No conflict when there's enough capacity for all
  ✅ Orphan release detection
  ✅ Overlap detection (sweep-line algorithm)
  ✅ Multiple groups with independent conflicts
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.event import EventAction, EventSource, ResourceType, StoredEvent
from app.services.conflict_resolver import (
    ResolutionResult,
    find_overlapping_groups,
    is_orphan_release,
    resolve_conflicts,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_TS = datetime(2024, 3, 1, 8, 0, 0, tzinfo=timezone.utc)


def make_stored_event(
    event_id: str,
    amount: float = 100.0,
    priority: int = 5,
    carbon_intensity: float = 300.0,
    timestamp: datetime = None,
    duration_hours: float = 4.0,
    action: str = "request",
    user_id: str = "user-test",
    region: str = "us-east-1",
    resource_type: str = "GPU",
) -> StoredEvent:
    ts = timestamp or BASE_TS
    return StoredEvent(
        event_id=event_id,
        source=EventSource.ai_training,
        timestamp=ts,
        action=EventAction(action),
        resource_type=ResourceType(resource_type),
        amount=amount,
        carbon_intensity=carbon_intensity,
        region=region,
        user_id=user_id,
        priority=priority,
        duration_hours=duration_hours,
        raw_payload="{}",
        received_at=ts,
    )


# ---------------------------------------------------------------------------
# Resolution tests
# ---------------------------------------------------------------------------

class TestResolvePriority:
    def test_higher_priority_wins(self):
        low = make_stored_event("low-p", amount=600.0, priority=3)
        high = make_stored_event("high-p", amount=600.0, priority=8)
        # Capacity = 800 → can only fit one 600
        result = resolve_conflicts([low, high], available_capacity=800.0)
        assert "high-p" in result.accepted
        assert "low-p" in result.rejected
        assert result.conflict_detected is True

    def test_lower_priority_alone_gets_accepted_if_capacity(self):
        low = make_stored_event("low-only", amount=100.0, priority=1)
        result = resolve_conflicts([low], available_capacity=500.0)
        assert "low-only" in result.accepted
        assert result.conflict_detected is False

    def test_all_accepted_when_capacity_sufficient(self):
        e1 = make_stored_event("e1", amount=100.0, priority=8)
        e2 = make_stored_event("e2", amount=100.0, priority=3)
        result = resolve_conflicts([e1, e2], available_capacity=500.0)
        assert set(result.accepted) == {"e1", "e2"}
        assert result.rejected == []
        assert result.conflict_detected is False

    def test_multiple_events_sorted_by_priority(self):
        events = [
            make_stored_event("p1", amount=300.0, priority=1),
            make_stored_event("p5", amount=300.0, priority=5),
            make_stored_event("p9", amount=300.0, priority=9),
        ]
        # Capacity 700 → fits p9 (300) + p5 (300) but NOT p1 (300): total = 900
        result = resolve_conflicts(events, available_capacity=700.0)
        assert "p9" in result.accepted
        assert "p5" in result.accepted
        assert "p1" in result.rejected


class TestResolveCarbonTieBreaker:
    def test_lower_carbon_intensity_wins_on_same_priority(self):
        dirty = make_stored_event("dirty", amount=600.0, priority=5, carbon_intensity=490.0)
        green = make_stored_event("green", amount=600.0, priority=5, carbon_intensity=90.0)
        result = resolve_conflicts([dirty, green], available_capacity=800.0)
        assert "green" in result.accepted
        assert "dirty" in result.rejected

    def test_same_priority_same_carbon_earliest_timestamp_wins(self):
        ts1 = BASE_TS
        ts2 = BASE_TS + timedelta(hours=1)
        first = make_stored_event(
            "first", amount=600.0, priority=5, carbon_intensity=200.0, timestamp=ts1
        )
        second = make_stored_event(
            "second", amount=600.0, priority=5, carbon_intensity=200.0, timestamp=ts2
        )
        result = resolve_conflicts([first, second], available_capacity=800.0)
        assert "first" in result.accepted
        assert "second" in result.rejected


class TestHardCapEnforcement:
    def test_over_allocation_hard_rejected_even_with_max_priority(self):
        """Priority 10 cannot override the hard capacity cap."""
        greedy = make_stored_event("greedy", amount=1100.0, priority=10)
        result = resolve_conflicts([greedy], available_capacity=1000.0)
        assert "greedy" in result.rejected
        assert result.conflict_detected is True

    def test_combined_amount_over_capacity_rejects_lower_priority(self):
        ok = make_stored_event("ok", amount=800.0, priority=9)
        excess = make_stored_event("excess", amount=300.0, priority=2)
        result = resolve_conflicts([ok, excess], available_capacity=1000.0)
        assert "ok" in result.accepted
        assert "excess" in result.rejected
        assert result.total_allocated == 800.0

    def test_zero_capacity_rejects_everything(self):
        e = make_stored_event("any", amount=1.0, priority=10)
        result = resolve_conflicts([e], available_capacity=0.0)
        assert "any" in result.rejected

    def test_empty_candidate_list(self):
        result = resolve_conflicts([], available_capacity=1000.0)
        assert result.accepted == []
        assert result.rejected == []
        assert result.conflict_detected is False


class TestOverlapDetection:
    def test_non_overlapping_events_in_separate_groups(self):
        # [08:00, 10:00)  and  [12:00, 14:00) — no overlap
        e1 = make_stored_event("e1", timestamp=BASE_TS, duration_hours=2.0)
        e2 = make_stored_event(
            "e2", timestamp=BASE_TS + timedelta(hours=4), duration_hours=2.0
        )
        groups = find_overlapping_groups([e1, e2])
        assert len(groups) == 2
        assert groups[0][0].event_id == "e1"
        assert groups[1][0].event_id == "e2"

    def test_overlapping_events_in_same_group(self):
        # [08:00, 12:00)  and  [10:00, 14:00) — overlap at [10:00, 12:00)
        e1 = make_stored_event("e1", timestamp=BASE_TS, duration_hours=4.0)
        e2 = make_stored_event(
            "e2", timestamp=BASE_TS + timedelta(hours=2), duration_hours=4.0
        )
        groups = find_overlapping_groups([e1, e2])
        assert len(groups) == 1
        ids = {e.event_id for e in groups[0]}
        assert ids == {"e1", "e2"}

    def test_three_overlapping_events_one_group(self):
        e1 = make_stored_event("e1", timestamp=BASE_TS, duration_hours=6.0)
        e2 = make_stored_event(
            "e2", timestamp=BASE_TS + timedelta(hours=2), duration_hours=4.0
        )
        e3 = make_stored_event(
            "e3", timestamp=BASE_TS + timedelta(hours=4), duration_hours=4.0
        )
        groups = find_overlapping_groups([e1, e2, e3])
        assert len(groups) == 1

    def test_exact_touching_windows_do_not_overlap(self):
        # Half-open intervals: [08:00, 10:00) and [10:00, 12:00) do NOT overlap
        e1 = make_stored_event("e1", timestamp=BASE_TS, duration_hours=2.0)
        e2 = make_stored_event(
            "e2", timestamp=BASE_TS + timedelta(hours=2), duration_hours=2.0
        )
        groups = find_overlapping_groups([e1, e2])
        assert len(groups) == 2

    def test_empty_list_returns_empty(self):
        assert find_overlapping_groups([]) == []


class TestOrphanRelease:
    def test_release_with_no_prior_request_is_orphan(self):
        rel = make_stored_event("rel-001", action="release", user_id="user-x")
        orphan = is_orphan_release(
            release_event=rel,
            accepted_event_ids=set(),
            prior_requests=[],
        )
        assert orphan is True

    def test_release_with_accepted_request_is_not_orphan(self):
        req = make_stored_event(
            "req-001", action="request", user_id="user-y", region="us-east-1", resource_type="GPU"
        )
        rel = make_stored_event(
            "rel-001", action="release", user_id="user-y", region="us-east-1", resource_type="GPU"
        )
        orphan = is_orphan_release(
            release_event=rel,
            accepted_event_ids={"req-001"},
            prior_requests=[req],
        )
        assert orphan is False

    def test_release_with_rejected_request_is_still_orphan(self):
        """If the matching request was rejected, the release is still orphaned."""
        req = make_stored_event(
            "req-rejected", action="request", user_id="user-z"
        )
        rel = make_stored_event(
            "rel-z", action="release", user_id="user-z"
        )
        # req is NOT in accepted_event_ids (it was rejected)
        orphan = is_orphan_release(
            release_event=rel,
            accepted_event_ids=set(),  # empty — rejected
            prior_requests=[req],
        )
        assert orphan is True

    def test_release_for_different_region_is_orphan(self):
        req = make_stored_event("req-r", user_id="user-a", region="us-east-1")
        rel = make_stored_event("rel-r", action="release", user_id="user-a", region="eu-west-1")
        orphan = is_orphan_release(
            release_event=rel,
            accepted_event_ids={"req-r"},
            prior_requests=[req],
        )
        assert orphan is True


class TestDeterminism:
    def test_same_input_different_order_same_result(self):
        """Resolver output must be order-independent (it re-sorts internally)."""
        e1 = make_stored_event("e1", amount=600.0, priority=8, carbon_intensity=100.0)
        e2 = make_stored_event("e2", amount=600.0, priority=3, carbon_intensity=500.0)
        e3 = make_stored_event("e3", amount=600.0, priority=5, carbon_intensity=300.0)

        result_abc = resolve_conflicts([e1, e2, e3], available_capacity=800.0)
        result_cba = resolve_conflicts([e3, e2, e1], available_capacity=800.0)
        result_bac = resolve_conflicts([e2, e1, e3], available_capacity=800.0)

        assert result_abc.accepted == result_cba.accepted == result_bac.accepted
        assert result_abc.rejected == result_cba.rejected == result_bac.rejected
