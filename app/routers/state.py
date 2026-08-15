"""
GET /state — current allocation state endpoints.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, status

from app.config import CAPACITY, SUPPORTED_REGIONS
from app.models.state import ResourceBucket, StateSnapshot
from app.services import state_reconstructor

router = APIRouter(prefix="/state", tags=["State"])

REGION_CAPACITIES = {
    "Mumbai": 100.0,
    "Delhi": 100.0,
    "Frankfurt": 100.0,
}


@router.get(
    "",
    response_model=StateSnapshot,
    summary="Get current allocation state",
    description=(
        "Returns the current versioned resource allocation state across all regions "
        "and resource types. This is a derived projection — rebuilt from the event log "
        "on each full replay. The snapshot is kept in sync incrementally after each event."
    ),
)
async def get_state() -> StateSnapshot:
    return state_reconstructor.get_current_state()


@router.get(
    "/at",
    response_model=StateSnapshot,
    summary="Get historical state at a specific timestamp",
    description="Reconstructs the versioned resource allocation state as it existed at the given ISO8601 timestamp.",
)
async def get_state_at(timestamp: str) -> StateSnapshot:
    try:
        target_dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid ISO-8601 timestamp: {timestamp!r}",
        )

    return state_reconstructor.get_state_at_timestamp(target_dt)


@router.get(
    "/summary",
    summary="Get resource utilization summary",
)
async def get_state_summary():
    snapshot = state_reconstructor.get_current_state()

    totals = {}
    for rtype in ["GPU", "CPU", "memory"]:
        allocated = sum(b.allocated_amount for b in snapshot.buckets if b.resource_type == rtype)
        cap_per_region = CAPACITY.get(rtype, 1000.0)
        capacity = cap_per_region * len(SUPPORTED_REGIONS)
        percentage = (allocated / capacity * 100.0) if capacity > 0 else 0.0
        totals[rtype] = {
            "allocated": round(allocated, 2),
            "capacity": round(capacity, 2),
            "percentage": round(percentage, 2),
        }

    all_regions = set(SUPPORTED_REGIONS) | set(b.region for b in snapshot.buckets) | set(REGION_CAPACITIES.keys())
    region_util = {}
    for region in all_regions:
        allocated = sum(b.allocated_amount for b in snapshot.buckets if b.region == region)
        capacity = REGION_CAPACITIES.get(region, 100.0)
        percentage = (allocated / capacity * 100.0) if capacity > 0 else 0.0
        region_util[region] = round(percentage, 2)

    return {
        "resource_totals": totals,
        "region_utilization": region_util,
    }


@router.get(
    "/{region}/{resource_type}",
    response_model=ResourceBucket,
    summary="Get allocation state for a specific bucket",
    description="Returns the allocation state for a single (region, resource_type) bucket.",
)
async def get_bucket_state(region: str, resource_type: str) -> ResourceBucket:
    snapshot = state_reconstructor.get_current_state()
    bucket = snapshot.get_bucket(region, resource_type)
    if bucket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No state found for region={region!r}, resource_type={resource_type!r}. "
                   "This bucket may not have received any events yet.",
        )
    return bucket
