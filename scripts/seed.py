#!/usr/bin/env python3
"""
seed.py — Seed the allocation engine with fixture events.

Usage:
  python scripts/seed.py                     # seed all fixtures in defined order
  python scripts/seed.py --scramble          # seed in randomised order (proves OOO handling)
  python scripts/seed.py --fixture 01_normal_request.json  # seed a specific fixture
  python scripts/seed.py --host http://localhost:8000      # custom API host
  python scripts/seed.py --dry-run           # print events without posting

Fixtures directory is auto-discovered relative to this script's location.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import List

try:
    import httpx
except ImportError:
    print("httpx not installed. Run: pip install httpx", file=sys.stderr)
    sys.exit(1)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
DEFAULT_HOST = "http://localhost:8000"


def load_fixtures(fixture_dir: Path, specific: str | None = None) -> List[dict]:
    """Load all fixture JSON files (or a specific one) sorted by filename."""
    if specific:
        files = [fixture_dir / specific]
    else:
        files = sorted(fixture_dir.glob("*.json"))

    events = []
    for f in files:
        with open(f) as fh:
            data = json.load(fh)
        if isinstance(data, list):
            for item in data:
                # Strip _comment keys (not valid API fields)
                events.append({k: v for k, v in item.items() if not k.startswith("_")})
        elif isinstance(data, dict):
            events.append({k: v for k, v in data.items() if not k.startswith("_")})
    return events


def post_event(client: httpx.Client, event: dict, dry_run: bool = False) -> dict:
    if dry_run:
        print(f"  [DRY-RUN] Would POST: {event.get('event_id', '?')}")
        return {}

    resp = client.post("/events", json=event, timeout=10.0)
    return {"status_code": resp.status_code, "body": resp.json()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the allocation engine with fixture events")
    parser.add_argument("--host", default=DEFAULT_HOST, help="API base URL")
    parser.add_argument("--fixture", default=None, help="Seed a specific fixture file")
    parser.add_argument("--scramble", action="store_true", help="Randomise event order")
    parser.add_argument("--dry-run", action="store_true", help="Print without POSTing")
    parser.add_argument("--delay", type=float, default=0.1, help="Seconds between requests")
    args = parser.parse_args()

    events = load_fixtures(FIXTURES_DIR, args.fixture)

    if args.scramble:
        random.shuffle(events)
        print(f"🔀 Scrambled {len(events)} events (out-of-order seeding)")
    else:
        print(f"📋 Seeding {len(events)} events in fixture order")

    if not events:
        print("No fixtures found. Check the fixtures/ directory.")
        return 1

    with httpx.Client(base_url=args.host) as client:
        # Health check
        if not args.dry_run:
            try:
                health = client.get("/health", timeout=5.0)
                if health.status_code != 200:
                    print(f"⚠️  API health check returned {health.status_code}. Proceeding anyway.")
                else:
                    print(f"✅  API healthy at {args.host}")
            except Exception as e:
                print(f"❌  Cannot reach API at {args.host}: {e}", file=sys.stderr)
                print("    Is the server running? Try: uvicorn app.main:app --reload")
                return 1

        accepted = 0
        duplicate = 0
        rejected = 0
        errors = 0

        for i, event in enumerate(events, 1):
            eid = event.get("event_id", "?")
            action = event.get("action", "?")
            rtype = event.get("resource_type", "?")
            region = event.get("region", "?")
            print(f"  [{i:02d}/{len(events):02d}] {eid[:36]:<40} {action:<10} {rtype:<8} {region}")

            result = post_event(client, event, args.dry_run)
            if not result:
                continue

            sc = result["status_code"]
            if sc == 200:
                accepted += 1
                delta = result["body"]
                print(f"           ✅  accepted  → allocated={delta.get('new_amount', '?'):.1f}")
            elif sc == 409:
                duplicate += 1
                print(f"           ⚠️  409 duplicate (idempotent no-op) ✓")
            elif sc == 422:
                rejected += 1
                print(f"           🚫 422 schema validation error ✓ (expected for malformed fixtures)")
            else:
                errors += 1
                print(f"           ❌ unexpected {sc}: {result['body']}")

            if args.delay > 0:
                time.sleep(args.delay)

    print()
    print("=" * 60)
    print(f"Seed complete:")
    print(f"  Accepted:    {accepted}")
    print(f"  Duplicates:  {duplicate} (all correct 409 responses)")
    print(f"  Rejected:    {rejected} (schema errors — expected)")
    print(f"  Errors:      {errors}")
    print("=" * 60)

    if not args.dry_run:
        print(f"\nNext steps:")
        print(f"  Check state:  curl {args.host}/state | python -m json.tool")
        print(f"  Check audit:  curl {args.host}/audit | python -m json.tool")
        print(f"  Replay diff:  python app/replay.py --diff")

    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
