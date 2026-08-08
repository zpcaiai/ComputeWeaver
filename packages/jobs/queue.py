from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from packages.persistence.postgres import PostgresRuntime


@dataclass(frozen=True, slots=True)
class DurableJob:
    id: int
    tenant_id: str
    kind: str
    payload: dict[str, Any]
    idempotency_key: str
    attempt: int
    max_attempts: int
    lease_expires_at: datetime


class PostgresJobQueue:
    def __init__(self, runtime: PostgresRuntime) -> None:
        self.runtime = runtime

    def enqueue(
        self,
        *,
        tenant_id: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        max_attempts: int = 5,
        available_at: datetime | None = None,
    ) -> int:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        with self.runtime.tenant_connection(tenant_id) as connection:
            existing = connection.execute(
                """
                SELECT id, kind, payload FROM durable_jobs
                WHERE tenant_id = %s AND idempotency_key = %s
                """,
                (tenant_id, idempotency_key),
            ).fetchone()
            if existing:
                if existing["kind"] != kind or json.dumps(existing["payload"], sort_keys=True) != json.dumps(
                    payload, sort_keys=True, default=str
                ):
                    raise ValueError("job idempotency key reused with different payload")
                return int(existing["id"])
            row = connection.execute(
                """
                INSERT INTO durable_jobs(
                  tenant_id, kind, payload, idempotency_key, max_attempts, available_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    tenant_id,
                    kind,
                    Jsonb(json.loads(json.dumps(payload, default=str))),
                    idempotency_key,
                    max_attempts,
                    available_at or datetime.now(UTC),
                ),
            ).fetchone()
            if not row:
                raise RuntimeError("job insert returned no identifier")
            return int(row["id"])

    def claim(self, *, worker_id: str, lease_seconds: int = 60) -> DurableJob | None:
        """Claim globally; the worker DB role must be explicitly granted BYPASSRLS."""
        if lease_seconds < 5:
            raise ValueError("lease must be at least five seconds")
        with self.runtime.connection() as connection:
            row = connection.execute(
                """
                WITH candidate AS (
                  SELECT id
                  FROM durable_jobs
                  WHERE (status = 'pending' AND available_at <= now())
                     OR (status = 'running' AND lease_expires_at <= now())
                  ORDER BY available_at, id
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
                )
                UPDATE durable_jobs AS job SET
                  status = 'running',
                  attempt = job.attempt + 1,
                  leased_by = %s,
                  lease_expires_at = now() + make_interval(secs => %s),
                  updated_at = now()
                FROM candidate
                WHERE job.id = candidate.id
                RETURNING job.id, job.tenant_id, job.kind, job.payload,
                          job.idempotency_key, job.attempt, job.max_attempts, job.lease_expires_at
                """,
                (worker_id, lease_seconds),
            ).fetchone()
            if not row:
                return None
            return DurableJob(
                int(row["id"]),
                str(row["tenant_id"]),
                str(row["kind"]),
                dict(row["payload"]),
                str(row["idempotency_key"]),
                int(row["attempt"]),
                int(row["max_attempts"]),
                row["lease_expires_at"],
            )

    def heartbeat(self, job: DurableJob, *, worker_id: str, lease_seconds: int = 60) -> bool:
        with self.runtime.tenant_connection(job.tenant_id) as connection:
            row = connection.execute(
                """
                UPDATE durable_jobs SET lease_expires_at = now() + make_interval(secs => %s), updated_at = now()
                WHERE id = %s AND status = 'running' AND leased_by = %s
                RETURNING id
                """,
                (lease_seconds, job.id, worker_id),
            ).fetchone()
            return row is not None

    def succeed(self, job: DurableJob, *, worker_id: str, result: dict[str, Any]) -> None:
        with self.runtime.tenant_connection(job.tenant_id) as connection:
            row = connection.execute(
                """
                UPDATE durable_jobs SET
                  status = 'succeeded', result = %s, leased_by = NULL,
                  lease_expires_at = NULL, updated_at = now()
                WHERE id = %s AND status = 'running' AND leased_by = %s
                RETURNING id
                """,
                (Jsonb(json.loads(json.dumps(result, default=str))), job.id, worker_id),
            ).fetchone()
            if not row:
                raise RuntimeError("job lease was lost before completion")

    def fail(self, job: DurableJob, *, worker_id: str, error: str) -> str:
        terminal = job.attempt >= job.max_attempts
        status = "dead_letter" if terminal else "pending"
        delay = min(300, 2 ** min(job.attempt, 8))
        with self.runtime.tenant_connection(job.tenant_id) as connection:
            row = connection.execute(
                """
                UPDATE durable_jobs SET
                  status = %s,
                  last_error = %s,
                  available_at = now() + %s,
                  leased_by = NULL,
                  lease_expires_at = NULL,
                  updated_at = now()
                WHERE id = %s AND status = 'running' AND leased_by = %s
                RETURNING status
                """,
                (status, error[:4000], timedelta(seconds=delay), job.id, worker_id),
            ).fetchone()
            if not row:
                raise RuntimeError("job lease was lost before failure recording")
            return str(row["status"])

    def status(self, tenant_id: str, job_id: int) -> dict[str, Any]:
        with self.runtime.tenant_connection(tenant_id) as connection:
            row = connection.execute(
                """
                SELECT id, kind, status, attempt, max_attempts, available_at,
                       lease_expires_at, last_error, result, created_at, updated_at
                FROM durable_jobs WHERE tenant_id = %s AND id = %s
                """,
                (tenant_id, job_id),
            ).fetchone()
            if not row:
                raise KeyError(job_id)
            return dict(row)
