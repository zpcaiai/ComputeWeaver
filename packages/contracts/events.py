from __future__ import annotations

import hashlib
import json
from datetime import datetime
from threading import RLock
from typing import Any

from pydantic import BaseModel, Field, field_validator

from packages.domain.identity import new_id, validate_id
from packages.domain.time import ensure_aware, utc_now


class EventEnvelope(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    event_type: str
    schema_version: str = "1.0.0"
    occurred_at: datetime = Field(default_factory=utc_now)
    observed_at: datetime = Field(default_factory=utc_now)
    tenant_id: str
    site_id: str
    trace_id: str
    payload: dict[str, Any]

    @field_validator("tenant_id", "site_id")
    @classmethod
    def identifiers_are_valid(cls, value: str) -> str:
        return validate_id(value)

    @field_validator("occurred_at", "observed_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return ensure_aware(value)

    def content_hash(self) -> str:
        body = self.model_dump(mode="json")
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class EventStore:
    """Thread-safe append-only event store with tenant-scoped reads."""

    def __init__(self) -> None:
        self._events: list[EventEnvelope] = []
        self._event_ids: set[str] = set()
        self._lock = RLock()

    def append(self, event: EventEnvelope) -> bool:
        with self._lock:
            if event.event_id in self._event_ids:
                return False
            self._events.append(event.model_copy(deep=True))
            self._event_ids.add(event.event_id)
            return True

    def query(self, tenant_id: str, *, event_type: str | None = None) -> tuple[EventEnvelope, ...]:
        with self._lock:
            return tuple(
                event.model_copy(deep=True)
                for event in self._events
                if event.tenant_id == tenant_id and (event_type is None or event.event_type == event_type)
            )

    def verify_chain(self) -> bool:
        with self._lock:
            return len(self._event_ids) == len(self._events)
