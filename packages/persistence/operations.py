from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import timedelta
from threading import RLock
from typing import Any

from psycopg.types.json import Jsonb

from .postgres import PostgresRuntime


class OperationIdempotency:
    """Crash-aware request idempotency for stateful API operations outside the resource store."""

    def __init__(self, runtime: PostgresRuntime | None = None, *, lease: timedelta = timedelta(minutes=2)) -> None:
        if lease <= timedelta(0):
            raise ValueError("operation lease must be positive")
        self.runtime = runtime
        self.lease = lease
        self._memory: dict[tuple[str, str], tuple[str, str, Any]] = {}
        self._lock = RLock()

    def execute_once(
        self,
        *,
        tenant_id: str,
        key: str,
        operation: str,
        intent: dict[str, Any],
        callback: Callable[[], Any],
        actor_id: str = "system",
    ) -> Any:
        if not tenant_id or len(key) < 8 or not operation:
            raise ValueError("tenant, operation and an idempotency key of at least 8 characters are required")
        digest = hashlib.sha256(json.dumps(intent, default=str, sort_keys=True).encode()).hexdigest()
        if not self.runtime:
            return self._execute_memory(tenant_id, key, operation, digest, callback)
        with self.runtime.tenant_connection(tenant_id, actor_id) as connection:
            row = connection.execute(
                """
                SELECT operation, intent_hash, status, response,
                       (status = 'started' AND lease_expires_at > now()) AS lease_active
                FROM api_operations
                WHERE tenant_id = %s AND idempotency_key = %s
                FOR UPDATE
                """,
                (tenant_id, key),
            ).fetchone()
            if row:
                self._check_identity(row, operation, digest)
                if row["status"] == "succeeded":
                    return row["response"]
                if row["lease_active"]:
                    raise RuntimeError("matching API operation is already in progress")
                connection.execute(
                    """
                    UPDATE api_operations SET
                      status = 'started', error = NULL, response = NULL,
                      lease_expires_at = now() + %s, started_at = now(), completed_at = NULL
                    WHERE tenant_id = %s AND idempotency_key = %s
                    """,
                    (self.lease, tenant_id, key),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO api_operations(
                      tenant_id, idempotency_key, operation, intent_hash, status, lease_expires_at
                    ) VALUES (%s, %s, %s, %s, 'started', now() + %s)
                    """,
                    (tenant_id, key, operation, digest, self.lease),
                )
        try:
            result = callback()
        except Exception as error:
            with self.runtime.tenant_connection(tenant_id, actor_id) as connection:
                connection.execute(
                    """
                    UPDATE api_operations SET status = 'failed', error = %s, completed_at = now()
                    WHERE tenant_id = %s AND idempotency_key = %s AND intent_hash = %s
                    """,
                    (f"{type(error).__name__}: {error}"[:4000], tenant_id, key, digest),
                )
            raise
        serializable = json.loads(json.dumps(result, default=str))
        with self.runtime.tenant_connection(tenant_id, actor_id) as connection:
            updated = connection.execute(
                """
                UPDATE api_operations SET status = 'succeeded', response = %s, completed_at = now()
                WHERE tenant_id = %s AND idempotency_key = %s
                  AND intent_hash = %s AND status = 'started'
                RETURNING idempotency_key
                """,
                (Jsonb(serializable), tenant_id, key, digest),
            ).fetchone()
            if not updated:
                raise RuntimeError("API operation lease was lost before completion")
        return result

    def _execute_memory(
        self,
        tenant_id: str,
        key: str,
        operation: str,
        digest: str,
        callback: Callable[[], Any],
    ) -> Any:
        with self._lock:
            identity = (tenant_id, key)
            existing = self._memory.get(identity)
            if existing:
                previous_operation, previous_digest, result = existing
                if previous_operation != operation or previous_digest != digest:
                    raise ValueError("idempotency key reused with a different API operation")
                return result
            result = callback()
            self._memory[identity] = (operation, digest, result)
            return result

    @staticmethod
    def _check_identity(row: dict[str, Any], operation: str, digest: str) -> None:
        if row["operation"] != operation or row["intent_hash"] != digest:
            raise ValueError("idempotency key reused with a different API operation")
