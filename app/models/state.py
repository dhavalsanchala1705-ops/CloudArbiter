"""
State models — versioned resource allocation snapshots.
"""
from __future__ import annotations

from typing import Dict, List, Optional
from pydantic import BaseModel


class ResourceBucket(BaseModel):
    """
    Current allocation state for a single (region, resource_type) bucket.
    """
    region: str
    resource_type: str
    allocated_amount: float = 0.0
    carbon_budget_used: float = 0.0
    version: int = 0
    last_event_id: Optional[str] = None

    @classmethod
    def from_row(cls, row) -> "ResourceBucket":
        return cls(**dict(row))


class StateSnapshot(BaseModel):
    """
    Full allocation state across all regions and resource types.
    This is the response body for GET /state.
    """
    buckets: List[ResourceBucket] = []
    total_events_applied: int = 0

    def get_bucket(self, region: str, resource_type: str) -> Optional[ResourceBucket]:
        for b in self.buckets:
            if b.region == region and b.resource_type == resource_type:
                return b
        return None

    def as_dict(self) -> Dict[str, Dict[str, float]]:
        """
        Return a nested dict keyed by region → resource_type → allocated_amount.
        Used for diffing in the replay script.
        """
        out: Dict[str, Dict[str, float]] = {}
        for b in self.buckets:
            out.setdefault(b.region, {})[b.resource_type] = b.allocated_amount
        return out


class StateDelta(BaseModel):
    """
    Returned by POST /events — shows what changed after processing the event.
    """
    event_id: str
    region: str
    resource_type: str
    previous_amount: float
    new_amount: float
    conflict_detected: bool = False
    audit_entry_id: Optional[str] = None
