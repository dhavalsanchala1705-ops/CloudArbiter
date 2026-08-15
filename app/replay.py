#!/usr/bin/env python3
"""
replay.py — Standalone replay and diff tool.

Usage:
  python app/replay.py --rebuild          Rebuild state from event log, print result
  python app/replay.py --diff             Rebuild + diff against current live state
  python app/replay.py --export           Export full event log + audit log as JSON
  python app/replay.py --rebuild --diff   Rebuild and check consistency (exit 0 = OK)

This script intentionally does NOT import the FastAPI app — it runs purely
against the SQLite database and the service layer, so it works whether the
API is running or not.

Exit codes:
  0  — success / states are identical
  1  — states differ (replay-consistency broken)
  2  — usage error
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure the project root is on the path when run from any directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import get_connection, init_db
from app.services import audit_engine, event_store, state_reconstructor


def cmd_rebuild(args) -> StateSnapshot:  # noqa: F821
    """Truncate state_snapshot + audit_log and rebuild from scratch."""
    conn = get_connection()
    init_db(conn)

    print("=" * 60)
    print("REPLAY: Rebuilding state from event log...")
    print("=" * 60)

    event_count = event_store.count_events(conn)
    print(f"  Events in log: {event_count}")

    snapshot = state_reconstructor.rebuild_state_from_events(conn)

    print(f"  Buckets reconstructed: {len(snapshot.buckets)}")
    print(f"  Total events applied:  {snapshot.total_events_applied}")
    print()
    print("Rebuilt state:")
    for b in snapshot.buckets:
        print(
            f"  [{b.region:>20}] {b.resource_type:<8} "
            f"allocated={b.allocated_amount:>10.2f}  "
            f"carbon_used={b.carbon_budget_used:>12.2f}  "
            f"version={b.version}"
        )

    audit_count = audit_engine.count_audit_entries(conn)
    print(f"\nAudit entries: {audit_count}")
    return snapshot


def cmd_diff() -> int:
    """
    Capture live state BEFORE rebuild, rebuild, compare AFTER.
    Returns 0 if identical, 1 if different.
    """
    conn = get_connection()
    init_db(conn)

    # Capture live state dict
    live = state_reconstructor.get_current_state(conn)
    live_dict = live.as_dict()

    print("Live state (before rebuild):")
    print(json.dumps(live_dict, indent=2, default=str))
    print()

    # Rebuild
    rebuilt = state_reconstructor.rebuild_state_from_events(conn)
    rebuilt_dict = rebuilt.as_dict()

    print("\nRebuilt state (after replay):")
    print(json.dumps(rebuilt_dict, indent=2, default=str))
    print()

    if live_dict == rebuilt_dict:
        print("✅  STATES ARE IDENTICAL — replay-consistency VERIFIED")
        return 0
    else:
        print("❌  STATES DIFFER — replay-consistency BROKEN")
        # Show diff
        all_keys = set(live_dict) | set(rebuilt_dict)
        for region in sorted(all_keys):
            live_r = live_dict.get(region, {})
            rebuilt_r = rebuilt_dict.get(region, {})
            all_types = set(live_r) | set(rebuilt_r)
            for rt in sorted(all_types):
                lv = live_r.get(rt, "MISSING")
                rv = rebuilt_r.get(rt, "MISSING")
                if lv != rv:
                    print(f"  DIFF [{region}][{rt}]: live={lv}  rebuilt={rv}")
        return 1


def cmd_export() -> None:
    """Export the full event log and audit log as JSON to stdout."""
    conn = get_connection()
    init_db(conn)

    events = [
        json.loads(e.raw_payload)
        for e in event_store.get_events_ordered(conn=conn)
    ]
    audit = [
        {
            "decision_id": a.decision_id,
            "event_ids_considered": a.event_ids_considered,
            "conflict_type": a.conflict_type.value,
            "resolution_reason": a.resolution_reason,
            "final_state": a.final_state,
            "decision_timestamp": a.decision_timestamp.isoformat(),
        }
        for a in audit_engine.get_audit_log(limit=10000, conn=conn)
    ]

    output = {
        "event_count": len(events),
        "audit_count": len(audit),
        "events": events,
        "audit_log": audit,
    }
    print(json.dumps(output, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay and diff tool for the Cloud Resource Allocation Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild state_snapshot and audit_log from the event log.",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Rebuild and diff against previous live state. Exit 0 if identical.",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Export full event log + audit log as JSON to stdout.",
    )

    args = parser.parse_args()

    if not any([args.rebuild, args.diff, args.export]):
        parser.print_help()
        return 2

    if args.export:
        cmd_export()
        return 0

    if args.diff:
        return cmd_diff()

    if args.rebuild:
        cmd_rebuild(args)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
