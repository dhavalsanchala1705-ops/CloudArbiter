"""
Audit log models — immutable decision records.
"""
from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any, List, Optional

from pydantic import BaseModel


class ConflictType(str, Enum):
    overlap_same_region_resource = "overlap_same_region_resource"
    over_allocation_attempt = "over_allocation_attempt"
    orphan_release = "orphan_release"
    no_conflict = "no_conflict"


class AuditEntry(BaseModel):
    """
    A single immutable decision record.
    Written once, never updated — mirrors the audit_log table.
    """
    decision_id: str
    event_ids_considered: List[str]
    conflict_type: ConflictType
    resolution_reason: str
    final_state: dict
    decision_timestamp: datetime

    @classmethod
    def from_row(cls, row: Any) -> "AuditEntry":
        d = dict(row)
        d["event_ids_considered"] = json.loads(d["event_ids_considered"])
        d["final_state"] = json.loads(d["final_state"])
        return cls(**d)

    def to_db_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "event_ids_considered": json.dumps(self.event_ids_considered),
            "conflict_type": self.conflict_type.value,
            "resolution_reason": self.resolution_reason,
            "final_state": json.dumps(self.final_state),
            "decision_timestamp": self.decision_timestamp.isoformat(),
        }
