# CloudArbiter — Cloud Resource Allocation Engine

Welcome to CloudArbiter — an event-sourced resource allocation and conflict-resolution engine with a built-in dashboard, explainability layer, and a chaos test lab for load and conflict simulation.

**Project Brief**
- **Purpose:** Deterministic, auditable allocation of cloud resources using event-sourcing. Conflicts between allocation requests are resolved by a conflict resolver; every decision is persisted to an audit log for replay and inspection.
- **Key ideas:** immutable event log, deterministic replay, explainability for decisions, and tools for stress-testing the ingestion pipeline.

**Repository layout**
- [app/main.py](app/main.py) — FastAPI application entrypoint and router registration
- [app/routers/](app/routers/) — API route modules (audit, events, chaos, state)
- [app/services/](app/services/) — core services (event_store, state_reconstructor, audit_engine, chaos generator, explainability)
- [static/](static/) — dashboard and UI pages (`index.html`, `explain.html`, `chaos.html`)
- [fixtures/](fixtures/) — example event fixtures used for seeding the DB
- [scripts/seed.py](scripts/seed.py) — script to populate the local DB with sample data
- [Dockerfile](Dockerfile) — container image build instructions
- [requirements.txt](requirements.txt) — Python dependencies

**Features**
- Event ingestion endpoint and validation
- Deterministic state reconstruction via event replay
- Conflict resolution with audit log of decisions and visualizations
- Explainability API: `GET /audit/{decision_id}/explain` (decision-level rationale)
- Chaos generator: `POST /chaos/run` and `GET /chaos/status` for live stress tests
- Dashboard UI with Theme (dark / light) toggle persisted in `localStorage`

**Tech stack**
- Python 3.11+ (project uses 3.11-slim in Dockerfile)
- FastAPI + Uvicorn
- Pydantic v2 for models/validation
- SQLite for local persistence (DB path: `data/allocation.db`)
- Vanilla JS + Tailwind CDN for static UI pages

**Quickstart — Local (recommended for development)**
1. Create a virtualenv and install deps:

```bash
python -m venv .venv
source .venv/Scripts/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

2. Seed sample data (optional):

```bash
python scripts/seed.py
```

3. Run the app:

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

4. Open the dashboard in your browser:

- http://127.0.0.1:8000/dashboard/
- Explainability UI: [static/explain.html](static/explain.html)
- Chaos Lab UI: [static/chaos.html](static/chaos.html)

**Docker / Compose**
- Build and run with Docker (exposes `:8000`):

```bash
docker build -t cloudarbiter:local .
docker run --rm -p 8000:8000 -v "${PWD}/data:/app/data" cloudarbiter:local
```

Or use `docker-compose` if you prefer (compose file provided at repository root).

**Important endpoints**
- POST `/events` — ingest an allocation event
- GET `/state` — current allocation state
- GET `/audit` — paginated audit log
- GET `/audit/{decision_id}` — single audit decision
- GET `/audit/{decision_id}/explain` — explainability payload for a decision
- POST `/chaos/run` — run a chaos load test (JSON body: `{events, duplicates, out_of_order, conflicts}`)
- GET `/chaos/status` — status & metrics for last chaos run

**Troubleshooting & notes**
- If port 8000 is in use on Windows, find and stop the process or choose another port:

```powershell
netstat -ano | findstr :8000
tasklist /FI "PID eq <pid>"
taskkill /PID <pid> /F
```

- The SQLite DB is located at `data/allocation.db`. Back it up if you intend to run destructive chaos tests.

**Contributing**
- Use branches and open pull requests to `main`. Tests live in `tests/` and can be run with `pytest -q`.

**License**
- MIT — see LICENSE file if present.

-----

If you'd like, I can:
- upload your local screenshots into `docs/screenshots/` for you (you must provide the image files), or
- generate smaller annotated thumbnails for the README.

To have me push the screenshots as well, upload the images (or place them in the repo) and tell me the filenames to use.
# Event-Driven Conflict Resolution for Sustainable Cloud Resource Allocation

A fully locally-deployable **event sourcing engine** that ingests asynchronous, possibly
out-of-order, duplicate, or conflicting resource allocation events and produces a
deterministic, replay-consistent state + immutable audit trail.

**No external cloud. No Kafka. No PostgreSQL. Pure Python + SQLite + Docker.**

---

## Architecture

```
POST /events  →  [Validate]  →  [Dedup / 409]  →  [Append to event log]
                                                          │
                                                    [Fold bucket]
                                                          │
                                              ┌───────────┴──────────┐
                                              ▼                      ▼
                                     [Conflict Resolver]      [State Snapshot]
                                      (pure function)          (derived cache)
                                              │
                                       [Audit Engine]
                                      (append-only log)
```

- **Event log** (`events` table) — immutable, append-only. Never UPDATE/DELETE.
- **State snapshot** (`state_snapshot` table) — derived projection. Safe to delete and rebuild.
- **Audit log** (`audit_log` table) — append-only decision records. Never UPDATE/DELETE.
- **Ordering key**: `(timestamp, event_id)` — never arrival order. This is why replay is deterministic.

### Conflict Resolution Rules (deterministic)

| Priority | Rule |
|----------|------|
| 1 | Higher `priority` integer wins (1–10) |
| 2 | Lower `carbon_intensity` wins (greener region) |
| 3 | Earlier `timestamp` wins (first-come-first-served) |
| 4 | **Hard cap**: total accepted ≤ available capacity (always) |
| 5 | Orphan releases rejected gracefully (no state corruption) |

---

## Quick Start (Docker)

```bash
# 1. Clone and build
git clone <repo-url>
cd myOnsite-project
docker compose up --build

# API is live at: http://localhost:8000
# Swagger docs:   http://localhost:8000/docs
# Dashboard:      http://localhost:8000/dashboard
```

---

## Local Development (without Docker)

```bash
# Create virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

# Install dependencies
pip install -r requirements.txt

# Create data directory
mkdir data

# Start the server
uvicorn app.main:app --reload --port 8000
```

---

## Seeding Events (Edge Cases)

```bash
# Seed all 7 edge-case fixtures in defined order
python scripts/seed.py

# Seed in scrambled/out-of-order sequence (proves OOO handling)
python scripts/seed.py --scramble

# Seed a single fixture
python scripts/seed.py --fixture 04_conflicting_same_priority.json

# Dry-run (print without POSTing)
python scripts/seed.py --dry-run

# Against a running Docker container
python scripts/seed.py --host http://localhost:8000
```

### Fixture Edge Cases

| File | Edge Case |
|------|-----------|
| `01_normal_request.json` | ✅ Baseline valid GPU request |
| `02_duplicate_event.json` | ⚠️ Same `event_id` as #01 → must 409, no state change |
| `03_out_of_order_events.json` | 🔀 3 events seeded in reverse timestamp order |
| `04_conflicting_same_priority.json` | ⚡ Same priority → `carbon_intensity` tie-breaker |
| `05_orphan_release.json` | 👻 Release with no prior allocation → graceful rejection |
| `06_malformed_event.json` | 🚫 Missing `resource_type` → 400, NOT stored in event log |
| `07_over_allocation.json` | ⛔ Exceeds capacity even at priority=10 → hard rejected |

---

## Running Tests

```bash
# Local
pytest -v

# Inside Docker
docker compose run app pytest -v --tb=short

# Specific test file
pytest tests/test_conflict_resolver.py -v
```

### Test Coverage

| File | What it tests |
|------|--------------|
| `test_ingestion.py` | Valid events, idempotency, schema validation (10 cases) |
| `test_conflict_resolver.py` | All 4 priority rules, overlap detection, orphan release, determinism |
| `test_state_reconstruction.py` | OOO handling, replay consistency, release semantics |
| `test_audit.py` | Audit completeness, pagination, orphan entries, required fields |

---

## Replay / Determinism Check

```bash
# Rebuild state from scratch and compare against live state
python app/replay.py --diff
# Exit 0 = identical (system is deterministic ✅)
# Exit 1 = differs   (system is broken ❌)

# Just rebuild (prints rebuilt state)
python app/replay.py --rebuild

# Export full event log + audit as JSON
python app/replay.py --export > export.json

# Via the API (also rebuilds + returns new state)
curl -X POST http://localhost:8000/admin/replay | python -m json.tool
```

---

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/events` | Ingest a resource allocation event |
| `GET`  | `/state` | Current allocation state (all buckets) |
| `GET`  | `/state/{region}/{resource_type}` | Single bucket state |
| `GET`  | `/audit` | Paginated audit log (`?limit=50&offset=0`) |
| `GET`  | `/audit/{decision_id}` | Single audit decision |
| `POST` | `/admin/replay` | Trigger full state rebuild from event log |
| `GET`  | `/health` | Health check |
| `GET`  | `/docs` | Swagger UI (interactive testing) |
| `GET`  | `/dashboard` | Live monitoring dashboard |

### Event Schema

```json
{
  "event_id":        "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "source":          "ai_training | serverless | sustainable",
  "timestamp":       "2024-03-01T08:00:00Z",
  "action":          "request | release",
  "resource_type":   "GPU | CPU | memory",
  "amount":          100.0,
  "carbon_intensity": 120.0,
  "region":          "us-west-2",
  "user_id":         "user-alice",
  "priority":        8,
  "duration_hours":  4.0
}
```

### Response Codes

| Code | Meaning |
|------|---------|
| `200` | Event accepted, state updated |
| `409` | Duplicate `event_id` — idempotent no-op, no state change |
| `422` | Schema validation error — event NOT stored in event log |
| `404` | State bucket or audit entry not found |

---

## Capacity Limits (configurable)

Override via environment variables in `docker-compose.yml`:

| Resource | Default | Env var |
|----------|---------|---------|
| GPU | 1000 hours/region | `GPU_CAPACITY_HOURS` |
| CPU | 5000 hours/region | `CPU_CAPACITY_HOURS` |
| memory | 10000 GB·hours/region | `MEM_CAPACITY_GBH` |
| Carbon budget | no hard cap | `CARBON_BUDGET_KG` |

---

## Database Schema

```sql
-- Immutable event log (never UPDATE/DELETE)
events (event_id PK, source, timestamp, action, resource_type,
        amount, carbon_intensity, region, user_id,
        priority, duration_hours, raw_payload, received_at)

-- Derived projection (safe to rebuild from events table)
state_snapshot (region, resource_type PK, allocated_amount,
                carbon_budget_used, version, last_event_id)

-- Immutable audit trail (never UPDATE/DELETE)
audit_log (decision_id PK, event_ids_considered, conflict_type,
           resolution_reason, final_state, decision_timestamp)
```

The SQLite file lives at `data/allocation.db` and is volume-mounted so it
persists across container restarts. Judges can inspect it directly with any
SQLite viewer (e.g. `sqlite3 data/allocation.db` or DB Browser for SQLite).

---

## Full Demo Walkthrough

```bash
# 1. Start
docker compose up --build

# 2. Seed all fixtures (including conflicts, duplicates, OOO events)
python scripts/seed.py

# 3. Check live state
curl http://localhost:8000/state | python -m json.tool

# 4. Check audit log
curl http://localhost:8000/audit | python -m json.tool

# 5. Seed again — all duplicates must return 409, state unchanged
python scripts/seed.py

# 6. Prove determinism: rebuild from scratch, diff against live
python app/replay.py --diff
# Expected output: ✅  STATES ARE IDENTICAL — replay-consistency VERIFIED

# 7. Open dashboard
open http://localhost:8000/dashboard   # or browser-navigate

# 8. Run tests inside Docker
docker compose run app pytest -v

# 9. Inspect the SQLite DB directly
sqlite3 data/allocation.db "SELECT * FROM audit_log;"
sqlite3 data/allocation.db "SELECT COUNT(*) FROM events;"
```

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.11 |
| API framework | FastAPI + Uvicorn |
| Validation | Pydantic v2 |
| Persistence | SQLite (WAL mode) via built-in `sqlite3` |
| AWS stubs | boto3 + moto (region metadata only) |
| Testing | pytest + pytest-asyncio + httpx |
| Containerisation | Docker + docker-compose |
| Dashboard | Vanilla HTML/CSS/JS |
