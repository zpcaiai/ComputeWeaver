from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any, Protocol

from psycopg.types.json import Jsonb

from packages.persistence.postgres import PostgresRuntime


@dataclass(frozen=True, slots=True)
class StoredResource:
    kind: str
    id: str
    tenant_id: str
    version: int
    etag: str
    body: dict[str, Any]


class Store(Protocol):
    def put(
        self,
        *,
        kind: str,
        resource_id: str,
        tenant_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        if_match: str | None = None,
    ) -> StoredResource: ...

    def get(self, kind: str, resource_id: str, tenant_id: str) -> StoredResource: ...

    def list(self, kind: str, tenant_id: str) -> tuple[StoredResource, ...]: ...

    def get_version(self, kind: str, resource_id: str, tenant_id: str, version: int) -> StoredResource: ...

    def history(self, kind: str, resource_id: str, tenant_id: str) -> tuple[StoredResource, ...]: ...

    def health(self) -> bool: ...


def _json_safe(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, default=str))


class ResourceStore:
    """Tenant-scoped in-process repository, restricted to test and simulator profiles."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], StoredResource] = {}
        self._versions: dict[tuple[str, str, str, int], StoredResource] = {}
        self._idempotency: dict[tuple[str, str], tuple[str, StoredResource]] = {}
        self._lock = RLock()

    def put(
        self,
        *,
        kind: str,
        resource_id: str,
        tenant_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        if_match: str | None = None,
    ) -> StoredResource:
        request_hash = hashlib.sha256(
            json.dumps(
                {"kind": kind, "resource_id": resource_id, "body": body},
                default=str,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self._lock:
            idempotency = (tenant_id, idempotency_key)
            if idempotency in self._idempotency:
                previous_hash, resource = self._idempotency[idempotency]
                if request_hash != previous_hash:
                    raise ValueError("idempotency key reused with different request")
                return resource
            key = (tenant_id, kind, resource_id)
            previous = self._items.get(key)
            if previous and if_match != previous.etag:
                raise RuntimeError("If-Match optimistic concurrency conflict")
            version = previous.version + 1 if previous else 1
            normalized = {**body, "id": resource_id}
            etag = hashlib.sha256(
                json.dumps(
                    {"tenant": tenant_id, "kind": kind, "version": version, "body": normalized},
                    default=str,
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            resource = StoredResource(kind, resource_id, tenant_id, version, etag, normalized)
            self._items[key] = resource
            self._versions[(tenant_id, kind, resource_id, version)] = resource
            self._idempotency[idempotency] = (request_hash, resource)
            return resource

    def get(self, kind: str, resource_id: str, tenant_id: str) -> StoredResource:
        return self._items[(tenant_id, kind, resource_id)]

    def list(self, kind: str, tenant_id: str) -> tuple[StoredResource, ...]:
        return tuple(
            sorted(
                (
                    item
                    for (item_tenant, item_kind, _), item in self._items.items()
                    if item_tenant == tenant_id and item_kind == kind
                ),
                key=lambda item: item.id,
            )
        )

    def get_version(self, kind: str, resource_id: str, tenant_id: str, version: int) -> StoredResource:
        return self._versions[(tenant_id, kind, resource_id, version)]

    def history(self, kind: str, resource_id: str, tenant_id: str) -> tuple[StoredResource, ...]:
        with self._lock:
            return tuple(
                self._versions[(tenant_id, kind, resource_id, version)]
                for version in sorted(
                    item_version
                    for item_tenant, item_kind, item_id, item_version in self._versions
                    if item_tenant == tenant_id and item_kind == kind and item_id == resource_id
                )
            )

    def health(self) -> bool:
        return True


class PostgresResourceStore:
    """Durable, RLS-scoped resource repository with transactional idempotency."""

    def __init__(self, runtime: PostgresRuntime, idempotency_ttl: timedelta = timedelta(hours=24)) -> None:
        self.runtime = runtime
        self.idempotency_ttl = idempotency_ttl

    @staticmethod
    def _from_response(response: dict[str, Any]) -> StoredResource:
        return StoredResource(
            kind=str(response["kind"]),
            id=str(response["id"]),
            tenant_id=str(response["tenant_id"]),
            version=int(response["version"]),
            etag=str(response["etag"]),
            body=dict(response["body"]),
        )

    @staticmethod
    def _response(resource: StoredResource) -> dict[str, Any]:
        return {
            "kind": resource.kind,
            "id": resource.id,
            "tenant_id": resource.tenant_id,
            "version": resource.version,
            "etag": resource.etag,
            "body": resource.body,
        }

    def put(
        self,
        *,
        kind: str,
        resource_id: str,
        tenant_id: str,
        body: dict[str, Any],
        idempotency_key: str,
        if_match: str | None = None,
    ) -> StoredResource:
        normalized = {**_json_safe(body), "id": resource_id}
        request_hash = hashlib.sha256(
            json.dumps(
                {"kind": kind, "resource_id": resource_id, "body": normalized},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self.runtime.tenant_connection(tenant_id) as connection:
            connection.execute(
                "DELETE FROM idempotency_records WHERE tenant_id = %s AND idempotency_key = %s AND expires_at <= now()",
                (tenant_id, idempotency_key),
            )
            replay = connection.execute(
                """
                SELECT request_hash, response_body
                FROM idempotency_records
                WHERE tenant_id = %s AND idempotency_key = %s
                FOR UPDATE
                """,
                (tenant_id, idempotency_key),
            ).fetchone()
            if replay:
                if replay["request_hash"] != request_hash:
                    raise ValueError("idempotency key reused with different request")
                return self._from_response(dict(replay["response_body"]))

            previous = connection.execute(
                """
                SELECT version, etag, body
                FROM resources
                WHERE tenant_id = %s AND kind = %s AND resource_id = %s
                FOR UPDATE
                """,
                (tenant_id, kind, resource_id),
            ).fetchone()
            if previous and if_match != previous["etag"]:
                raise RuntimeError("If-Match optimistic concurrency conflict")
            version = int(previous["version"]) + 1 if previous else 1
            etag = hashlib.sha256(
                json.dumps(
                    {"tenant": tenant_id, "kind": kind, "version": version, "body": normalized},
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            resource = StoredResource(kind, resource_id, tenant_id, version, etag, normalized)
            connection.execute(
                """
                INSERT INTO resources(tenant_id, kind, resource_id, version, etag, body)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, kind, resource_id) DO UPDATE SET
                  version = EXCLUDED.version,
                  etag = EXCLUDED.etag,
                  body = EXCLUDED.body,
                  updated_at = now()
                """,
                (tenant_id, kind, resource_id, version, etag, Jsonb(normalized)),
            )
            connection.execute(
                """
                INSERT INTO resource_versions(tenant_id, kind, resource_id, version, etag, body)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (tenant_id, kind, resource_id, version, etag, Jsonb(normalized)),
            )
            expires_at = datetime.now(UTC) + self.idempotency_ttl
            connection.execute(
                """
                INSERT INTO idempotency_records(
                  tenant_id, idempotency_key, request_hash, response_status, response_body, expires_at
                ) VALUES (%s, %s, %s, 200, %s, %s)
                """,
                (
                    tenant_id,
                    idempotency_key,
                    request_hash,
                    Jsonb(self._response(resource)),
                    expires_at,
                ),
            )
            return resource

    def get(self, kind: str, resource_id: str, tenant_id: str) -> StoredResource:
        with self.runtime.tenant_connection(tenant_id) as connection:
            row = connection.execute(
                """
                SELECT version, etag, body
                FROM resources
                WHERE tenant_id = %s AND kind = %s AND resource_id = %s
                """,
                (tenant_id, kind, resource_id),
            ).fetchone()
            if not row:
                # A same-kind identifier owned by another tenant must not be disclosed.
                raise KeyError(resource_id)
            return StoredResource(
                kind,
                resource_id,
                tenant_id,
                int(row["version"]),
                str(row["etag"]),
                dict(row["body"]),
            )

    def list(self, kind: str, tenant_id: str) -> tuple[StoredResource, ...]:
        with self.runtime.tenant_connection(tenant_id) as connection:
            rows = connection.execute(
                """
                SELECT resource_id, version, etag, body
                FROM resources
                WHERE tenant_id = %s AND kind = %s
                ORDER BY resource_id
                """,
                (tenant_id, kind),
            ).fetchall()
            return tuple(
                StoredResource(
                    kind,
                    str(row["resource_id"]),
                    tenant_id,
                    int(row["version"]),
                    str(row["etag"]),
                    dict(row["body"]),
                )
                for row in rows
            )

    def get_version(self, kind: str, resource_id: str, tenant_id: str, version: int) -> StoredResource:
        with self.runtime.tenant_connection(tenant_id) as connection:
            row = connection.execute(
                """
                SELECT etag, body
                FROM resource_versions
                WHERE tenant_id = %s AND kind = %s AND resource_id = %s AND version = %s
                """,
                (tenant_id, kind, resource_id, version),
            ).fetchone()
            if not row:
                raise KeyError(f"{resource_id}@{version}")
            return StoredResource(kind, resource_id, tenant_id, version, str(row["etag"]), dict(row["body"]))

    def history(self, kind: str, resource_id: str, tenant_id: str) -> tuple[StoredResource, ...]:
        with self.runtime.tenant_connection(tenant_id) as connection:
            rows = connection.execute(
                """
                SELECT version, etag, body
                FROM resource_versions
                WHERE tenant_id = %s AND kind = %s AND resource_id = %s
                ORDER BY version
                """,
                (tenant_id, kind, resource_id),
            ).fetchall()
            return tuple(
                StoredResource(
                    kind,
                    resource_id,
                    tenant_id,
                    int(row["version"]),
                    str(row["etag"]),
                    dict(row["body"]),
                )
                for row in rows
            )

    def health(self) -> bool:
        return self.runtime.health()
