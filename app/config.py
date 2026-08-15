"""
Central configuration for the allocation engine.
All capacity limits and region metadata are defined here so they
can be overridden via environment variables at container start-time.
"""
import os
from typing import Dict

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH: str = os.getenv("DB_PATH", "data/allocation.db")

# ---------------------------------------------------------------------------
# Capacity limits  (hours / GB-hours per region)
# Override via env: GPU_CAPACITY_HOURS, CPU_CAPACITY_HOURS, MEM_CAPACITY_GBH
# ---------------------------------------------------------------------------
CAPACITY: Dict[str, float] = {
    "GPU": float(os.getenv("GPU_CAPACITY_HOURS", "1000")),
    "CPU": float(os.getenv("CPU_CAPACITY_HOURS", "5000")),
    "memory": float(os.getenv("MEM_CAPACITY_GBH", "10000")),
}

# ---------------------------------------------------------------------------
# Regions supported (used for validation + carbon-intensity defaults)
# ---------------------------------------------------------------------------
SUPPORTED_REGIONS = [
    "us-east-1",
    "us-west-2",
    "eu-west-1",
    "eu-central-1",
    "ap-southeast-1",
]

# Default carbon intensities (gCO₂/kWh) per region — used as fallback
# when the incoming event does not supply its own carbon_intensity.
DEFAULT_CARBON_INTENSITY: Dict[str, float] = {
    "us-east-1": 415.0,
    "us-west-2": 120.0,   # hydro-heavy
    "eu-west-1": 316.0,
    "eu-central-1": 411.0,
    "ap-southeast-1": 493.0,
}

# ---------------------------------------------------------------------------
# Carbon budget cap per region (kg CO₂ — None means no hard cap)
# The spec uses carbon_intensity as a tie-breaker; we also track
# carbon_budget_used in state but do NOT enforce a hard cap by default.
# Set CARBON_BUDGET_KG env var to a positive float to enable a hard cap.
# ---------------------------------------------------------------------------
_carbon_env = os.getenv("CARBON_BUDGET_KG")
CARBON_BUDGET_KG: float | None = float(_carbon_env) if _carbon_env else None

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
API_TITLE = "Cloud Resource Allocation Engine"
API_VERSION = "1.0.0"
API_DESCRIPTION = (
    "Event-driven conflict resolution for sustainable cloud resource allocation. "
    "Fully event-sourced — state is always a deterministic fold of the event log."
)
