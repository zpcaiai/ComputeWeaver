from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Any

from psycopg.types.json import Jsonb

from packages.persistence.postgres import PostgresRuntime


@dataclass(frozen=True, slots=True)
class RawEvent:
    id: str
    tenant_id: str
    source: str
    received_at: datetime
    payload: dict[str, Any]
    payload_hash: str

    @classmethod
    def create(
        cls, *, id: str, tenant_id: str, source: str, received_at: datetime, payload: dict[str, Any]
    ) -> RawEvent:
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return cls(id, tenant_id, source, received_at, dict(payload), digest)


class RawLanding:
    def __init__(self, runtime: PostgresRuntime | None = None) -> None:
        self._runtime = runtime
        self._events: dict[tuple[str, str], RawEvent] = {}
        self._ordered: list[tuple[str, str]] = []
        self._lock = RLock()

    def append(self, event: RawEvent) -> bool:
        if self._runtime:
            with self._runtime.tenant_connection(event.tenant_id) as connection:
                row = connection.execute(
                    """
                    INSERT INTO raw_events(tenant_id, event_id, source, received_at, payload, payload_hash)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, event_id) DO NOTHING
                    RETURNING event_id
                    """,
                    (
                        event.tenant_id,
                        event.id,
                        event.source,
                        event.received_at,
                        Jsonb(event.payload),
                        event.payload_hash,
                    ),
                ).fetchone()
                if row:
                    return True
                persisted = connection.execute(
                    "SELECT payload_hash FROM raw_events WHERE tenant_id = %s AND event_id = %s",
                    (event.tenant_id, event.id),
                ).fetchone()
                if not persisted or persisted["payload_hash"] != event.payload_hash:
                    raise ValueError("event ID collision with different payload")
                return False
        with self._lock:
            key = (event.tenant_id, event.id)
            if key in self._events:
                memory_event = self._events[key]
                if memory_event.payload_hash != event.payload_hash:
                    raise ValueError("event ID collision with different payload")
                return False
            self._events[key] = event
            self._ordered.append(key)
            return True

    def get(self, event_id: str, tenant_id: str) -> RawEvent:
        if self._runtime:
            with self._runtime.tenant_connection(tenant_id) as connection:
                row = connection.execute(
                    """
                    SELECT source, received_at, payload, payload_hash
                    FROM raw_events WHERE tenant_id = %s AND event_id = %s
                    """,
                    (tenant_id, event_id),
                ).fetchone()
                if not row:
                    raise KeyError(event_id)
                return RawEvent(
                    event_id,
                    tenant_id,
                    str(row["source"]),
                    row["received_at"],
                    dict(row["payload"]),
                    str(row["payload_hash"]),
                )
        return self._events[(tenant_id, event_id)]

    def query(self, tenant_id: str) -> tuple[RawEvent, ...]:
        if self._runtime:
            with self._runtime.tenant_connection(tenant_id) as connection:
                rows = connection.execute(
                    """
                    SELECT event_id, source, received_at, payload, payload_hash
                    FROM raw_events WHERE tenant_id = %s ORDER BY received_at, event_id
                    """,
                    (tenant_id,),
                ).fetchall()
                return tuple(
                    RawEvent(
                        str(row["event_id"]),
                        tenant_id,
                        str(row["source"]),
                        row["received_at"],
                        dict(row["payload"]),
                        str(row["payload_hash"]),
                    )
                    for row in rows
                )
        return tuple(self._events[item] for item in self._ordered if item[0] == tenant_id)
