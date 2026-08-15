"""
Tests for state reconstruction — replay consistency and out-of-order handling.

Key invariants tested:
  ✅ Out-of-order arrival → same final state regardless of ingestion order
  ✅ Full rebuild from event log → byte-identical state to incremental live state
  ✅ Release after request → correctly decrements allocation (min floor = 0)
  ✅ Multiple buckets are independent
  ✅ Rebuild is idempotent (can run multiple times → same result)
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.services import state_reconstructor
from tests.conftest import make_event


class TestOutOfOrderHandling:
    def test_out_of_order_arrival_same_state(self, client: TestClient, fresh_db):
        """
        Events seeded in reverse timestamp order must produce the same final state
        as if they were seeded in chronological order.
        """
        # Events with timestamps: A < B < C
        event_a = make_event(
            event_id="ooo-a",
            timestamp="2024-01-01T08:00:00Z",
            region="us-west-2",
            resource_type="CPU",
            amount=100.0,
            user_id="user-a",
        )
        event_b = make_event(
            event_id="ooo-b",
            timestamp="2024-01-01T09:00:00Z",
            region="us-west-2",
            resource_type="CPU",
            amount=200.0,
            user_id="user-b",
        )
        event_c = make_event(
            event_id="ooo-c",
            timestamp="2024-01-01T10:00:00Z",
            region="us-west-2",
            resource_type="CPU",
            amount=150.0,
            user_id="user-c",
        )

        # Seed in reverse order: C, B, A
        client.post("/events", json=event_c)
        client.post("/events", json=event_b)
        client.post("/events", json=event_a)

        # Get live state
        live_state = client.get("/state/us-west-2/CPU").json()

        # Rebuild from scratch (replay)
        rebuilt = state_reconstructor.rebuild_state_from_events(fresh_db)
        rebuilt_bucket = rebuilt.get_bucket("us-west-2", "CPU")

        assert rebuilt_bucket is not None
        assert rebuilt_bucket.allocated_amount == live_state["allocated_amount"]

    def test_ordering_by_event_id_as_tiebreaker(self, client: TestClient, fresh_db):
        """
        Two events with the same timestamp — event_id (lexicographic) is the
        deterministic tiebreaker.
        """
        ts = "2024-01-15T12:00:00Z"
        e_aaa = make_event(
            event_id="aaa-first",
            timestamp=ts,
            region="eu-west-1",
            resource_type="GPU",
            amount=600.0,
            user_id="user-aaa",
        )
        e_zzz = make_event(
            event_id="zzz-second",
            timestamp=ts,
            region="eu-west-1",
            resource_type="GPU",
            amount=600.0,
            user_id="user-zzz",
        )

        # Seed in reverse lexicographic order: zzz first, then aaa
        client.post("/events", json=e_zzz)
        client.post("/events", json=e_aaa)

        live = client.get("/state/eu-west-1/GPU").json()
        rebuilt = state_reconstructor.rebuild_state_from_events(fresh_db)
        rebuilt_bucket = rebuilt.get_bucket("eu-west-1", "GPU")

        assert rebuilt_bucket.allocated_amount == live["allocated_amount"]


class TestReplayConsistency:
    def test_rebuild_matches_live_state(self, client: TestClient, fresh_db):
        """Core correctness proof: rebuild == live state."""
        events = [
            make_event(event_id=f"replay-{i}", amount=float(i * 10 + 50),
                       region="us-east-1", resource_type="GPU",
                       user_id=f"user-{i}", priority=i % 10 + 1,
                       timestamp=f"2024-02-0{i+1}T10:00:00Z")
            for i in range(1, 6)
        ]
        for ev in events:
            client.post("/events", json=ev)

        live = state_reconstructor.get_current_state(fresh_db)
        rebuilt = state_reconstructor.rebuild_state_from_events(fresh_db)

        assert live.as_dict() == rebuilt.as_dict()

    def test_rebuild_is_idempotent(self, client: TestClient, fresh_db):
        """Multiple rebuilds should produce identical results."""
        events = [
            make_event(event_id=f"idem-{i}", amount=100.0,
                       region="ap-southeast-1", resource_type="CPU",
                       user_id=f"user-{i}")
            for i in range(3)
        ]
        for ev in events:
            client.post("/events", json=ev)

        first = state_reconstructor.rebuild_state_from_events(fresh_db)
        second = state_reconstructor.rebuild_state_from_events(fresh_db)
        third = state_reconstructor.rebuild_state_from_events(fresh_db)

        assert first.as_dict() == second.as_dict() == third.as_dict()

    def test_admin_replay_endpoint_returns_consistent_state(self, client: TestClient):
        events = [
            make_event(event_id=f"admin-{i}", amount=100.0,
                       region="us-west-2", resource_type="memory",
                       user_id=f"u{i}", timestamp=f"2024-03-0{i+1}T08:00:00Z")
            for i in range(3)
        ]
        for ev in events:
            client.post("/events", json=ev)

        pre_state = client.get("/state").json()
        replay_resp = client.post("/admin/replay")
        assert replay_resp.status_code == 200

        post_state = client.get("/state").json()
        # Allocated amounts must be identical before and after replay
        pre_dict = {
            (b["region"], b["resource_type"]): b["allocated_amount"]
            for b in pre_state["buckets"]
        }
        post_dict = {
            (b["region"], b["resource_type"]): b["allocated_amount"]
            for b in post_state["buckets"]
        }
        assert pre_dict == post_dict


class TestReleaseSemantics:
    def test_release_decrements_allocation(self, client: TestClient):
        req = make_event(
            event_id="rr-req-001",
            action="request",
            amount=300.0,
            region="eu-central-1",
            resource_type="CPU",
            user_id="user-release",
            timestamp="2024-04-01T08:00:00Z",
        )
        client.post("/events", json=req)

        rel = make_event(
            event_id="rr-rel-001",
            action="release",
            amount=300.0,
            region="eu-central-1",
            resource_type="CPU",
            user_id="user-release",
            timestamp="2024-04-01T14:00:00Z",
        )
        client.post("/events", json=rel)

        state = client.get("/state/eu-central-1/CPU").json()
        assert state["allocated_amount"] == 0.0

    def test_partial_release_leaves_remainder(self, client: TestClient):
        req = make_event(
            event_id="partial-req",
            action="request",
            amount=500.0,
            region="us-east-1",
            resource_type="memory",
            user_id="user-partial",
            timestamp="2024-05-01T08:00:00Z",
        )
        client.post("/events", json=req)

        rel = make_event(
            event_id="partial-rel",
            action="release",
            amount=200.0,
            region="us-east-1",
            resource_type="memory",
            user_id="user-partial",
            timestamp="2024-05-01T10:00:00Z",
        )
        client.post("/events", json=rel)

        state = client.get("/state/us-east-1/memory").json()
        assert state["allocated_amount"] == 300.0

    def test_allocation_never_goes_negative(self, client: TestClient):
        """Releasing more than allocated floors at 0, not negative."""
        req = make_event(
            event_id="neg-req",
            action="request",
            amount=100.0,
            region="us-west-2",
            resource_type="GPU",
            user_id="user-neg",
        )
        client.post("/events", json=req)

        # Release twice as much — should floor at 0
        rel = make_event(
            event_id="neg-rel",
            action="release",
            amount=200.0,
            region="us-west-2",
            resource_type="GPU",
            user_id="user-neg",
        )
        client.post("/events", json=rel)

        state = client.get("/state/us-west-2/GPU").json()
        assert state["allocated_amount"] >= 0.0


class TestMultipleBuckets:
    def test_buckets_are_independent(self, client: TestClient):
        """Events in one (region, resource_type) do not affect another bucket."""
        e1 = make_event(
            event_id="bucket-gpu",
            resource_type="GPU",
            region="us-east-1",
            amount=500.0,
            user_id="user-1",
        )
        e2 = make_event(
            event_id="bucket-cpu",
            resource_type="CPU",
            region="us-east-1",
            amount=300.0,
            user_id="user-2",
        )
        client.post("/events", json=e1)
        client.post("/events", json=e2)

        gpu = client.get("/state/us-east-1/GPU").json()
        cpu = client.get("/state/us-east-1/CPU").json()

        assert gpu["allocated_amount"] == 500.0
        assert cpu["allocated_amount"] == 300.0
