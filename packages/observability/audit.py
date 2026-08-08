from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from threading import RLock

from packages.domain.time import utc_now
from packages.persistence.postgres import PostgresRuntime


@dataclass(frozen=True, slots=True)
class AuditRecord:
    timestamp: datetime
    actor_id: str
    tenant_id: str
    action: str
    resource: str
    outcome: str
    correlation_id: str
    previous_hash: str
    record_hash: str


class AuditLog:
    def __init__(self, runtime: PostgresRuntime | None = None) -> None:
        self._runtime = runtime
        self._records: list[AuditRecord] = []
        self._lock = RLock()

    def append(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        action: str,
        resource: str,
        outcome: str,
        correlation_id: str,
    ) -> AuditRecord:
        if self._runtime:
            return self._append_postgres(
                actor_id=actor_id,
                tenant_id=tenant_id,
                action=action,
                resource=resource,
                outcome=outcome,
                correlation_id=correlation_id,
            )
        with self._lock:
            previous = self._records[-1].record_hash if self._records else "GENESIS"
            timestamp = utc_now()
            source = json.dumps(
                {
                    "timestamp": timestamp.isoformat(),
                    "actor_id": actor_id,
                    "tenant_id": tenant_id,
                    "action": action,
                    "resource": resource,
                    "outcome": outcome,
                    "correlation_id": correlation_id,
                    "previous_hash": previous,
                },
                sort_keys=True,
            )
            digest = hashlib.sha256(source.encode()).hexdigest()
            record = AuditRecord(
                timestamp,
                actor_id,
                tenant_id,
                action,
                resource,
                outcome,
                correlation_id,
                previous,
                digest,
            )
            self._records.append(record)
            return record

    def _append_postgres(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        action: str,
        resource: str,
        outcome: str,
        correlation_id: str,
    ) -> AuditRecord:
        timestamp = utc_now()
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("persistent audit runtime is unavailable")
        with runtime.tenant_connection(tenant_id, actor_id) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (tenant_id,))
            last = connection.execute(
                "SELECT record_hash FROM audit_records WHERE tenant_id = %s ORDER BY sequence DESC LIMIT 1",
                (tenant_id,),
            ).fetchone()
            previous = str(last["record_hash"]) if last else "GENESIS"
            source = json.dumps(
                {
                    "timestamp": timestamp.isoformat(),
                    "actor_id": actor_id,
                    "tenant_id": tenant_id,
                    "action": action,
                    "resource": resource,
                    "outcome": outcome,
                    "correlation_id": correlation_id,
                    "previous_hash": previous,
                },
                sort_keys=True,
            )
            digest = hashlib.sha256(source.encode()).hexdigest()
            connection.execute(
                """
                INSERT INTO audit_records(
                  tenant_id, actor_id, action, resource, outcome, correlation_id,
                  previous_hash, record_hash, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    actor_id,
                    action,
                    resource,
                    outcome,
                    correlation_id,
                    previous,
                    digest,
                    timestamp,
                ),
            )
        return AuditRecord(
            timestamp,
            actor_id,
            tenant_id,
            action,
            resource,
            outcome,
            correlation_id,
            previous,
            digest,
        )

    def verify(self) -> bool:
        if self._runtime:
            raise ValueError("tenant_id is required to verify a persistent audit chain")
        previous = "GENESIS"
        for record in self._records:
            source = asdict(record)
            digest = source.pop("record_hash")
            source["timestamp"] = record.timestamp.isoformat()
            if source["previous_hash"] != previous:
                return False
            if hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest() != digest:
                return False
            previous = digest
        return True

    def records(self, tenant_id: str) -> tuple[AuditRecord, ...]:
        if self._runtime:
            with self._runtime.tenant_connection(tenant_id) as connection:
                rows = connection.execute(
                    """
                    SELECT created_at, actor_id, tenant_id, action, resource, outcome,
                           correlation_id, previous_hash, record_hash
                    FROM audit_records
                    WHERE tenant_id = %s
                    ORDER BY sequence
                    """,
                    (tenant_id,),
                ).fetchall()
                return tuple(
                    AuditRecord(
                        row["created_at"].astimezone(UTC),
                        str(row["actor_id"]),
                        str(row["tenant_id"]),
                        str(row["action"]),
                        str(row["resource"]),
                        str(row["outcome"]),
                        str(row["correlation_id"]),
                        str(row["previous_hash"]),
                        str(row["record_hash"]),
                    )
                    for row in rows
                )
        return tuple(record for record in self._records if record.tenant_id == tenant_id)

    def verify_tenant(self, tenant_id: str) -> bool:
        previous = "GENESIS"
        for record in self.records(tenant_id):
            source = asdict(record)
            digest = source.pop("record_hash")
            source["timestamp"] = record.timestamp.isoformat()
            if source["previous_hash"] != previous:
                return False
            if hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest() != digest:
                return False
            previous = str(digest)
        return True
