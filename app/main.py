"""
FastAPI application entry point.

Lifespan:
  - On startup: initialise the SQLite database (create tables if not exist)
  - On shutdown: nothing special needed (SQLite handles cleanup)

Routes:
  POST /events         — ingest allocation events
  GET  /state          — current allocation state
  GET  /state/{r}/{t}  — single bucket state
  GET  /audit          — paginated audit log
  GET  /audit/{id}     — single audit decision
  POST /admin/replay   — trigger full state rebuild (for demo/testing)
  GET  /health         — health check (used by Docker healthcheck)
  GET  /               — serve optional dashboard (if static/ exists)
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import API_DESCRIPTION, API_TITLE, API_VERSION
from app.db import get_connection, init_db
from app.models.state import StateSnapshot
from app.routers import audit, events, state
from app.routers import chaos
from app.services import state_reconstructor


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the database on startup."""
    conn = get_connection()
    init_db(conn)
    yield
    # Nothing to tear down — SQLite connections are managed per-thread


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

app = FastAPI(
    title=API_TITLE,
    version=API_VERSION,
    description=API_DESCRIPTION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS — allows the optional static dashboard to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(events.router)
app.include_router(state.router)
app.include_router(audit.router)
app.include_router(chaos.router)


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@app.post(
    "/admin/replay",
    response_model=StateSnapshot,
    tags=["Admin"],
    summary="Trigger full state rebuild from event log",
    description=(
        "Discards the current state_snapshot projection and rebuilds it deterministically "
        "from the immutable event log. The resulting state MUST be identical to the "
        "previous state — this is the replay-consistency guarantee. "
        "Also rebuilds the audit log from scratch."
    ),
)
async def trigger_replay() -> StateSnapshot:
    return state_reconstructor.rebuild_state_from_events()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get(
    "/health",
    tags=["Health"],
    summary="Health check",
    description="Returns 200 OK when the service is running and the DB is reachable.",
)
async def health_check() -> Dict[str, Any]:
    try:
        conn = get_connection()
        row = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        event_count = row[0] if row else 0
        return {
            "status": "healthy",
            "event_count": event_count,
            "version": API_VERSION,
        }
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "error": str(exc)},
        )


# ---------------------------------------------------------------------------
# Global error handlers
# ---------------------------------------------------------------------------

@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)},
    )


# ---------------------------------------------------------------------------
# Static dashboard (optional — served only if static/index.html exists)
# ---------------------------------------------------------------------------

_static_dir = Path(__file__).parent.parent / "static"
if _static_dir.exists():
    app.mount("/dashboard", StaticFiles(directory=str(_static_dir), html=True), name="dashboard")
