"""
Pydantic v2 models for events.

IncomingEvent   — validated API input (what the caller POSTs)
StoredEvent     — row from the `events` table (adds received_at, etc.)
IngestResult    — result enum returned by the event store
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class EventSource(str, Enum):
    ai_training = "ai_training"
    serverless = "serverless"
    sustainable = "sustainable"


class EventAction(str, Enum):
    request = "request"
    release = "release"


class ResourceType(str, Enum):
    GPU = "GPU"
    CPU = "CPU"
    memory = "memory"


class IngestStatus(str, Enum):
    accepted = "accepted"
    duplicate = "duplicate"


# ---------------------------------------------------------------------------
# Incoming (API input) model
# ---------------------------------------------------------------------------

class IncomingEvent(BaseModel):
    """
    Schema for a cloud resource allocation event.
    All fields are required except priority (defaults to 5) and
    duration_hours (defaults to 1.0).
    """

    event_id: str = Field(
        ...,
        description="Globally unique event identifier (UUID recommended). "
                    "Used as the idempotency key — duplicate event_ids are rejected.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    source: EventSource = Field(
        ...,
        description="System that generated this event.",
    )
    timestamp: datetime = Field(
        ...,
        description="Event logical time (ISO-8601). Used for deterministic ordering, "
                    "NOT the server arrival time.",
    )
    action: EventAction = Field(
        ...,
        description="'request' to allocate resources, 'release' to free them.",
    )
    resource_type: ResourceType = Field(
        ...,
        description="Type of resource being requested or released.",
    )
    amount: float = Field(
        ...,
        gt=0,
        description="Amount of resource (hours for GPU/CPU, GB-hours for memory).",
    )
    carbon_intensity: float = Field(
        ...,
        ge=0,
        description="Carbon intensity of the target region in gCO₂/kWh. "
                    "Lower = greener. Used as tie-breaker in conflict resolution.",
    )
    region: str = Field(
        ...,
        min_length=1,
        description="Target deployment region (e.g. 'us-east-1').",
    )
    user_id: str = Field(
        ...,
        min_length=1,
        description="Identifier of the requesting user or service account.",
    )
    priority: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Allocation priority (1=lowest, 10=highest). "
                    "Higher priority wins conflicts. AI training jobs typically use 7-10.",
    )
    duration_hours: float = Field(
        default=1.0,
        gt=0,
        description="Expected duration of resource use (hours). "
                    "Used for conflict overlap detection: "
                    "window = [timestamp, timestamp + duration_hours).",
    )

    @field_validator("timestamp", mode="before")
    @classmethod
    def parse_timestamp(cls, v: Any) -> datetime:
        if isinstance(v, datetime):
            return v
        try:
            return datetime.fromisoformat(str(v))
        except ValueError as exc:
            raise ValueError(f"Invalid ISO-8601 timestamp: {v!r}") from exc

    @model_validator(mode="after")
    def release_must_have_matching_info(self) -> "IncomingEvent":
        """
        Release events must carry the same identifiers so the reconstructor
        can find the matching request.  No additional fields needed right now —
        placeholder for future cross-field validation.
        """
        return self

    def to_raw_payload(self) -> str:
        """Serialise to canonical JSON string for storage in raw_payload column."""
        return self.model_dump_json()


# ---------------------------------------------------------------------------
# Stored event (DB row representation)
# ---------------------------------------------------------------------------

class StoredEvent(BaseModel):
    """
    Mirrors a row in the `events` table.
    Constructed by the event store when reading back from SQLite.
    """

    event_id: str
    source: EventSource
    timestamp: datetime
    action: EventAction
    resource_type: ResourceType
    amount: float
    carbon_intensity: float
    region: str
    user_id: str
    priority: int
    duration_hours: float
    raw_payload: str
    received_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> "StoredEvent":
        """Construct from a sqlite3.Row object."""
        d = dict(row)
        return cls(**d)

    def window_start(self) -> datetime:
        return self.timestamp

    def window_end(self) -> datetime:
        from datetime import timedelta
        return self.timestamp + timedelta(hours=self.duration_hours)

    def overlaps(self, other: "StoredEvent") -> bool:
        """
        True if this event's time window overlaps with `other`'s window.
        Uses half-open intervals: [start, end).
        """
        return self.window_start() < other.window_end() and other.window_start() < self.window_end()


# ---------------------------------------------------------------------------
# Ingest result
# ---------------------------------------------------------------------------

class IngestResult(BaseModel):
    status: IngestStatus
    event_id: str
    message: str
