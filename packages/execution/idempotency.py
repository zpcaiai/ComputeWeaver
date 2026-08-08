from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from threading import RLock
from typing import Any

from psycopg.types.json import Jsonb

from packages.persistence.postgres import PostgresRuntime


class IdempotencyStore:
    def __init__(self, runtime: PostgresRuntime | None = None) -> None:
        self._runtime = runtime
        self._results: dict[tuple[str, str], tuple[str, Any]] = {}
        self._action_status: dict[tuple[str, str], str] = {}
        self._cancellations: dict[tuple[str, str], dict[str, str]] = {}
        self._lock = RLock()

    def execute_once(
        self,
        key: str,
        intent: dict[str, Any],
        operation: Callable[[], Any],
        *,
        tenant_id: str | None = None,
        action_id: str | None = None,
    ) -> Any:
        digest = hashlib.sha256(json.dumps(intent, sort_keys=True, default=str).encode()).hexdigest()
        if self._runtime:
            if not tenant_id or not action_id:
                raise ValueError("tenant_id and action_id are required for persistent execution")
            with self._runtime.tenant_connection(tenant_id) as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"{tenant_id}:{action_id}",),
                )
                cancelled = connection.execute(
                    "SELECT 1 FROM action_cancellations WHERE tenant_id = %s AND action_id = %s",
                    (tenant_id, action_id),
                ).fetchone()
                if cancelled:
                    raise PermissionError("action was cancelled before execution")
                row = connection.execute(
                    """
                    SELECT intent_hash, status, result FROM action_executions
                    WHERE tenant_id = %s AND idempotency_key = %s
                    FOR UPDATE
                    """,
                    (tenant_id, key),
                ).fetchone()
                if row:
                    if row["intent_hash"] != digest:
                        raise ValueError("idempotency key reused with different intent")
                    if row["status"] == "succeeded":
                        return row["result"]
                    if row["status"] == "started":
                        raise RuntimeError("matching action execution is already in progress")
                    connection.execute(
                        """
                        UPDATE action_executions SET status = 'started', error = NULL,
                          started_at = now(), completed_at = NULL
                        WHERE tenant_id = %s AND idempotency_key = %s
                        """,
                        (tenant_id, key),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO action_executions(
                          tenant_id, idempotency_key, action_id, intent_hash, status
                        ) VALUES (%s, %s, %s, %s, 'started')
                        """,
                        (tenant_id, key, action_id, digest),
                    )
            try:
                result = operation()
            except Exception as error:
                with self._runtime.tenant_connection(tenant_id) as connection:
                    connection.execute(
                        """
                        UPDATE action_executions SET status = 'failed', error = %s, completed_at = now()
                        WHERE tenant_id = %s AND idempotency_key = %s AND intent_hash = %s
                        """,
                        (f"{type(error).__name__}: {error}"[:4000], tenant_id, key, digest),
                    )
                raise
            serializable = json.loads(json.dumps(result, default=str))
            with self._runtime.tenant_connection(tenant_id) as connection:
                connection.execute(
                    """
                    UPDATE action_executions SET status = 'succeeded', result = %s, completed_at = now()
                    WHERE tenant_id = %s AND idempotency_key = %s AND intent_hash = %s
                    """,
                    (Jsonb(serializable), tenant_id, key, digest),
                )
            return result
        scope = tenant_id or "memory"
        identity = (scope, key)
        action_identity = (scope, action_id or str(intent.get("action", "unknown")))
        with self._lock:
            if action_identity in self._cancellations:
                raise PermissionError("action was cancelled before execution")
            if identity in self._results:
                previous_digest, result = self._results[identity]
                if previous_digest != digest:
                    raise ValueError("idempotency key reused with different intent")
                return result
            if self._action_status.get(action_identity) == "started":
                raise RuntimeError("matching action execution is already in progress")
            self._action_status[action_identity] = "started"
        try:
            result = operation()
        except Exception:
            with self._lock:
                self._action_status[action_identity] = "failed"
            raise
        with self._lock:
            self._results[identity] = (digest, result)
            self._action_status[action_identity] = "succeeded"
            return result

    def cancel(self, action_id: str, *, tenant_id: str, actor_id: str, reason: str) -> dict[str, str]:
        if not action_id or not tenant_id or not actor_id or not reason.strip():
            raise ValueError("action cancellation requires action, tenant, actor and reason")
        cancellation = {
            "action_id": action_id,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "reason": reason.strip(),
            "status": "cancelled",
        }
        if self._runtime:
            with self._runtime.tenant_connection(tenant_id, actor_id) as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"{tenant_id}:{action_id}",),
                )
                active = connection.execute(
                    """
                    SELECT status FROM action_executions
                    WHERE tenant_id = %s AND action_id = %s AND status IN ('started', 'succeeded')
                    LIMIT 1
                    """,
                    (tenant_id, action_id),
                ).fetchone()
                if active:
                    raise ValueError(f"action cannot be cancelled after status {active['status']}")
                existing = connection.execute(
                    "SELECT actor_id, reason FROM action_cancellations WHERE tenant_id = %s AND action_id = %s",
                    (tenant_id, action_id),
                ).fetchone()
                if existing:
                    if existing["actor_id"] != actor_id or existing["reason"] != reason.strip():
                        raise ValueError("action cancellation already exists with different intent")
                    return cancellation
                connection.execute(
                    """
                    INSERT INTO action_cancellations(tenant_id, action_id, actor_id, reason)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (tenant_id, action_id, actor_id, reason.strip()),
                )
            return cancellation
        identity = (tenant_id, action_id)
        with self._lock:
            if self._action_status.get(identity) in {"started", "succeeded"}:
                raise ValueError(f"action cannot be cancelled after status {self._action_status[identity]}")
            existing = self._cancellations.get(identity)
            if existing and existing != cancellation:
                raise ValueError("action cancellation already exists with different intent")
            self._cancellations[identity] = cancellation
            return dict(cancellation)
