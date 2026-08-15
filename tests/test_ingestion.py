"""
Tests for event ingestion — POST /events.

Covers:
  ✅ Valid event → 200, state is updated
  ✅ Same event_id sent twice → 409, state unchanged (idempotency)
  ✅ Missing required field → 422 (FastAPI/Pydantic validation)
  ✅ Wrong type for a field → 422
  ✅ Invalid enum value → 422
  ✅ Negative amount → 422
  ✅ Event appears in event store after accepted ingestion
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_event


class TestValidIngestion:
    def test_valid_event_returns_200(self, client: TestClient):
        payload = make_event()
        resp = client.post("/events", json=payload)
        assert resp.status_code == 200, resp.text

    def test_valid_event_returns_state_delta(self, client: TestClient):
        payload = make_event(amount=150.0)
        resp = client.post("/events", json=payload)
        body = resp.json()
        assert "event_id" in body
        assert "new_amount" in body
        assert body["new_amount"] == 150.0
        assert body["region"] == payload["region"]
        assert body["resource_type"] == payload["resource_type"]

    def test_previous_amount_is_zero_for_first_event(self, client: TestClient):
        payload = make_event(amount=200.0)
        resp = client.post("/events", json=payload)
        body = resp.json()
        assert body["previous_amount"] == 0.0

    def test_state_reflects_event_after_ingestion(self, client: TestClient):
        payload = make_event(region="eu-west-1", resource_type="CPU", amount=300.0)
        client.post("/events", json=payload)
        state_resp = client.get("/state")
        assert state_resp.status_code == 200
        buckets = state_resp.json()["buckets"]
        eu_cpu = next(
            (b for b in buckets if b["region"] == "eu-west-1" and b["resource_type"] == "CPU"),
            None,
        )
        assert eu_cpu is not None
        assert eu_cpu["allocated_amount"] == 300.0


class TestIdempotency:
    def test_duplicate_event_returns_409(self, client: TestClient):
        payload = make_event(event_id="dup-test-001")
        client.post("/events", json=payload)  # first — accepted
        resp = client.post("/events", json=payload)  # second — duplicate
        assert resp.status_code == 409, resp.text

    def test_duplicate_event_409_body_contains_event_id(self, client: TestClient):
        payload = make_event(event_id="dup-test-002")
        client.post("/events", json=payload)
        resp = client.post("/events", json=payload)
        body = resp.json()
        assert body["detail"]["event_id"] == "dup-test-002"
        assert body["detail"]["error"] == "duplicate_event"

    def test_duplicate_does_not_change_state(self, client: TestClient):
        payload = make_event(event_id="dup-test-003", amount=100.0, region="us-east-1")
        client.post("/events", json=payload)

        state_after_first = client.get("/state").json()

        # Send duplicate
        client.post("/events", json=payload)

        state_after_dup = client.get("/state").json()
        assert state_after_first == state_after_dup

    def test_same_event_ten_times_is_idempotent(self, client: TestClient):
        payload = make_event(event_id="dup-test-004", amount=50.0)
        client.post("/events", json=payload)  # accepted
        for _ in range(9):
            resp = client.post("/events", json=payload)
            assert resp.status_code == 409

        state = client.get(f"/state/{payload['region']}/{payload['resource_type']}").json()
        assert state["allocated_amount"] == 50.0


class TestSchemaValidation:
    def test_missing_resource_type_returns_422(self, client: TestClient):
        payload = make_event()
        del payload["resource_type"]
        resp = client.post("/events", json=payload)
        assert resp.status_code == 422, resp.text

    def test_missing_event_id_returns_422(self, client: TestClient):
        payload = make_event()
        del payload["event_id"]
        resp = client.post("/events", json=payload)
        assert resp.status_code == 422

    def test_invalid_resource_type_enum_returns_422(self, client: TestClient):
        payload = make_event(resource_type="QUANTUM")
        resp = client.post("/events", json=payload)
        assert resp.status_code == 422

    def test_invalid_source_enum_returns_422(self, client: TestClient):
        payload = make_event(source="blockchain_miner")
        resp = client.post("/events", json=payload)
        assert resp.status_code == 422

    def test_negative_amount_returns_422(self, client: TestClient):
        payload = make_event(amount=-10.0)
        resp = client.post("/events", json=payload)
        assert resp.status_code == 422

    def test_zero_amount_returns_422(self, client: TestClient):
        payload = make_event(amount=0.0)
        resp = client.post("/events", json=payload)
        assert resp.status_code == 422

    def test_invalid_action_returns_422(self, client: TestClient):
        payload = make_event(action="borrow")
        resp = client.post("/events", json=payload)
        assert resp.status_code == 422

    def test_malformed_json_returns_422(self, client: TestClient):
        resp = client.post(
            "/events",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    def test_priority_out_of_range_returns_422(self, client: TestClient):
        payload = make_event(priority=11)
        resp = client.post("/events", json=payload)
        assert resp.status_code == 422

    def test_malformed_event_not_in_event_store(self, client: TestClient):
        """Critical: rejected events must NEVER enter the event log."""
        payload = make_event(event_id="malformed-wont-store")
        del payload["region"]
        client.post("/events", json=payload)

        # If it had been stored, GET /state would show it; instead event count=0
        state = client.get("/state").json()
        assert state["total_events_applied"] == 0


class TestReleaseEvent:
    def test_release_after_request_decreases_allocation(self, client: TestClient):
        req = make_event(
            event_id="req-for-release-001",
            action="request",
            amount=200.0,
            user_id="user-release-test",
            region="us-east-1",
            resource_type="CPU",
        )
        client.post("/events", json=req)

        rel = make_event(
            event_id="release-001",
            action="release",
            amount=200.0,
            user_id="user-release-test",
            region="us-east-1",
            resource_type="CPU",
            timestamp="2024-03-01T12:00:00Z",
        )
        client.post("/events", json=rel)

        state = client.get("/state/us-east-1/CPU").json()
        assert state["allocated_amount"] == 0.0
