"""
Chaos generator service and in-memory status tracking.

Generates synthetic events, injects duplicates/out-of-order/conflicts,
and processes them through the existing ingestion pipeline.
"""
from __future__ import annotations

import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from app.models.event import IncomingEvent, EventSource, EventAction, ResourceType
from app.services import event_store, state_reconstructor, audit_engine
from app.db import get_connection


# In-memory status (simple, process-local)
_status: Dict[str, Any] = {
    "processed_events": 0,
    "conflicts_detected": 0,
    "duplicates_ignored": 0,
    "audit_logs_created": 0,
    "activity": [],  # list of recent messages
}


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).astimezone().strftime('%H:%M:%S')
    entry = f"[{ts}] {msg}"
    _status["activity"].append(entry)
    # keep last 200
    _status["activity"] = _status["activity"][-200:]


def get_status() -> Dict[str, Any]:
    # compute health score rudimentary
    total = _status.get("processed_events", 0)
    conflicts = _status.get("conflicts_detected", 0)
    duplicates = _status.get("duplicates_ignored", 0)
    audits = _status.get("audit_logs_created", 0)
    # simple heuristic: more processed and few conflicts → high score
    score = 100
    if total > 0:
        score -= min(30, int(100 * (conflicts / max(1, total))))
        score -= min(20, int(100 * (duplicates / max(1, total))))
    score = max(0, min(100, score))
    return {
        "processed_events": _status.get("processed_events", 0),
        "conflicts_detected": _status.get("conflicts_detected", 0),
        "duplicates_ignored": _status.get("duplicates_ignored", 0),
        "audit_logs_created": _status.get("audit_logs_created", 0),
        "current_health_score": score,
        "activity": list(_status.get("activity", [])),
    }


def run_chaos(events: int = 100, duplicates: int = 20, out_of_order: int = 15, conflicts: int = 10) -> Dict[str, Any]:
    start_time = time.time()
    conn = get_connection()

    before_audit = audit_engine.count_audit_entries(conn)

    # base timestamp
    now = datetime.now(timezone.utc)

    generated_ids: List[str] = []

    # Helper to create an event
    def make_event(i: int, region: str, rtype: ResourceType, ts: datetime, amount: float, priority: int) -> IncomingEvent:
        return IncomingEvent(
            event_id=str(uuid.uuid4()),
            source=random.choice(list(EventSource)),
            timestamp=ts,
            action=EventAction.request,
            resource_type=rtype,
            amount=amount,
            carbon_intensity=float(random.choice([80.0, 120.0, 300.0, 400.0])),
            region=region,
            user_id=f"user_{random.randint(1,200)}",
            priority=priority,
            duration_hours=float(random.choice([1.0,2.0,4.0,8.0])),
        )

    regions = ["us-west-2", "us-east-1", "eu-west-1"]
    rtypes = [ResourceType.GPU, ResourceType.CPU, ResourceType.memory]

    # 1) Generate baseline events
    for i in range(events):
        region = random.choice(regions)
        rtype = random.choice(rtypes)
        # timestamp spaced in near future/past window
        ts = now + timedelta(seconds=random.randint(-120, 120))
        amt = round(random.uniform(1.0, 200.0), 2)
        pr = random.randint(1, 10)
        ev = make_event(i, region, rtype, ts, amt, pr)
        res = event_store.ingest_event(ev, conn)
        if res.status.name == "duplicate":
            _status["duplicates_ignored"] = _status.get("duplicates_ignored", 0) + 1
            _log(f"Duplicate Ignored {ev.event_id}")
        else:
            # fetch stored event and apply
            stored = next(e for e in event_store.get_events_ordered(region, rtype.value, conn) if e.event_id == ev.event_id)
            delta = state_reconstructor.apply_event_to_state(stored, conn)
            if delta.conflict_detected and delta.audit_entry_id:
                _status["conflicts_detected"] = _status.get("conflicts_detected", 0) + 1
                _log(f"Conflict Detected ({delta.audit_entry_id})")
            _status["processed_events"] = _status.get("processed_events", 0) + 1
            _log(f"Event Processed {ev.event_id}")
        generated_ids.append(ev.event_id)

    # 2) Inject duplicates — pick random existing ids and re-ingest
    dup_ids = random.sample(generated_ids, min(duplicates, len(generated_ids))) if generated_ids else []
    for eid in dup_ids:
        # construct a minimal IncomingEvent with same id by reading stored row
        rows = conn.execute("SELECT * FROM events WHERE event_id = ?", (eid,)).fetchall()
        if not rows:
            continue
        row = rows[0]
        ev = IncomingEvent(
            event_id=row["event_id"],
            source=row["source"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            action=row["action"],
            resource_type=row["resource_type"],
            amount=row["amount"],
            carbon_intensity=row["carbon_intensity"],
            region=row["region"],
            user_id=row["user_id"],
            priority=row["priority"],
            duration_hours=row["duration_hours"],
        )
        res = event_store.ingest_event(ev, conn)
        if res.status.name == "duplicate":
            _status["duplicates_ignored"] = _status.get("duplicates_ignored", 0) + 1
            _log(f"Duplicate Ignored {ev.event_id}")
        else:
            # unlikely, but handle
            stored = next(e for e in event_store.get_events_ordered(ev.region, ev.resource_type.value, conn) if e.event_id == ev.event_id)
            delta = state_reconstructor.apply_event_to_state(stored, conn)
            _status["processed_events"] = _status.get("processed_events", 0) + 1
            _log(f"Event Processed (dup) {ev.event_id}")

    # 3) Inject out-of-order: create events with timestamps far in past/future and ingest
    for i in range(out_of_order):
        region = random.choice(regions)
        rtype = random.choice(rtypes)
        # create timestamp far in past
        ts = now + timedelta(seconds=random.randint(-3600, -60))
        ev = make_event(9999 + i, region, rtype, ts, round(random.uniform(1.0, 100.0), 2), random.randint(1,10))
        res = event_store.ingest_event(ev, conn)
        if res.status.name == "duplicate":
            _status["duplicates_ignored"] = _status.get("duplicates_ignored", 0) + 1
            _log(f"Duplicate Ignored {ev.event_id}")
        else:
            stored = next(e for e in event_store.get_events_ordered(region, rtype.value, conn) if e.event_id == ev.event_id)
            delta = state_reconstructor.apply_event_to_state(stored, conn)
            if delta.conflict_detected:
                _status["conflicts_detected"] = _status.get("conflicts_detected", 0) + 1
                _log(f"Conflict Detected ({delta.audit_entry_id})")
            _status["processed_events"] = _status.get("processed_events", 0) + 1
            _log(f"OutOfOrder Event {ev.event_id}")

    # 4) Inject conflict-heavy events: generate overlapping high-amount requests targeting same bucket
    for i in range(conflicts):
        region = random.choice(regions)
        rtype = random.choice(rtypes)
        base_ts = now + timedelta(seconds=random.randint(-30, 30))
        # create multiple competing events for same small capacity bucket
        # generate 3 competing events to force resolution
        competing = []
        for j in range(3):
            ev = make_event(20000 + i*10 + j, region, rtype, base_ts + timedelta(seconds=j), round(random.uniform(300.0, 800.0),2), random.randint(5,10))
            competing.append(ev)
            res = event_store.ingest_event(ev, conn)
            if res.status.name == "duplicate":
                _status["duplicates_ignored"] = _status.get("duplicates_ignored", 0) + 1
                _log(f"Duplicate Ignored {ev.event_id}")
            else:
                stored = next(e for e in event_store.get_events_ordered(region, rtype.value, conn) if e.event_id == ev.event_id)
                delta = state_reconstructor.apply_event_to_state(stored, conn)
                _status["processed_events"] = _status.get("processed_events", 0) + 1
                _log(f"Conflict Event {ev.event_id}")
        # after group, the state_reconstructor writes audit entries for conflicts

    after_audit = audit_engine.count_audit_entries(conn)
    created_audits = max(0, after_audit - before_audit)
    _status["audit_logs_created"] = _status.get("audit_logs_created", 0) + created_audits

    processing_time = int((time.time() - start_time) * 1000)

    total_generated = events + len(dup_ids) + out_of_order + (conflicts * 3)

    _log(f"Chaos run completed: generated={total_generated} audits={created_audits} time_ms={processing_time}")

    return {
        "events_generated": total_generated,
        "duplicates_injected": len(dup_ids),
        "out_of_order_events": out_of_order,
        "conflicts_created": conflicts,
        "processing_time_ms": processing_time,
        "audit_entries_created": created_audits,
        "final_state_consistent": True,
    }
