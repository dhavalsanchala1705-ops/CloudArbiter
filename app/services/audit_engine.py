"""
Audit Engine — writes immutable decision records to the audit_log table.

Rules:
  - NEVER UPDATE or DELETE audit records.
  - Every conflict resolution produces exactly one AuditEntry.
  - decision_id is a UUID4 generated at write time.
  - The full final_state snapshot is embedded as JSON for self-contained auditability.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from app.db import get_connection
from app.models.audit import AuditEntry, ConflictType


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def write_audit_entry(
    event_ids_considered: List[str],
    conflict_type: ConflictType,
    resolution_reason: str,
    final_state: dict,
    conn: Optional[sqlite3.Connection] = None,
) -> AuditEntry:
    """
    Append one immutable audit record to audit_log.

    Args:
        event_ids_considered:  All event_ids that were part of this decision.
        conflict_type:         Structured conflict taxonomy value.
        resolution_reason:     Human-readable explanation (includes rule id).
        final_state:           JSON-serialisable snapshot of the bucket state
                               AFTER the resolution was applied.

    Returns:
        The written AuditEntry (with generated decision_id and timestamp).
    """
    c = conn or get_connection()

    entry = AuditEntry(
        decision_id=str(uuid.uuid4()),
        event_ids_considered=event_ids_considered,
        conflict_type=conflict_type,
        resolution_reason=resolution_reason,
        final_state=final_state,
        decision_timestamp=datetime.now(timezone.utc),
    )

    db_dict = entry.to_db_dict()
    c.execute(
        """
        INSERT INTO audit_log
            (decision_id, event_ids_considered, conflict_type,
             resolution_reason, final_state, decision_timestamp)
        VALUES (:decision_id, :event_ids_considered, :conflict_type,
                :resolution_reason, :final_state, :decision_timestamp)
        """,
        db_dict,
    )
    c.commit()
    return entry


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_audit_log(
    limit: int = 50,
    offset: int = 0,
    conn: Optional[sqlite3.Connection] = None,
) -> List[AuditEntry]:
    """
    Return paginated audit entries ordered by decision_timestamp ascending.
    """
    c = conn or get_connection()
    rows = c.execute(
        """
        SELECT * FROM audit_log
        ORDER BY decision_timestamp ASC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    return [AuditEntry.from_row(r) for r in rows]


def get_audit_entry(
    decision_id: str,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[AuditEntry]:
    """Fetch a single audit entry by decision_id. Returns None if not found."""
    c = conn or get_connection()
    row = c.execute(
        "SELECT * FROM audit_log WHERE decision_id = ?", (decision_id,)
    ).fetchone()
    return AuditEntry.from_row(row) if row else None


def count_audit_entries(conn: Optional[sqlite3.Connection] = None) -> int:
    c = conn or get_connection()
    row = c.execute("SELECT COUNT(*) FROM audit_log").fetchone()
    return row[0] if row else 0
