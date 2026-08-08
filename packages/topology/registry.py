from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from threading import RLock

from psycopg.types.json import Jsonb

from packages.domain.time import TimeInterval, utc_now
from packages.persistence.postgres import PostgresRuntime

from .models import Asset, AssetType, Relationship, TopologySnapshot
from .validation import descendants, validate_graph


class TopologyRegistry:
    def __init__(self, runtime: PostgresRuntime | None = None) -> None:
        self._runtime = runtime
        self._drafts: dict[str, tuple[tuple[Asset, ...], tuple[Relationship, ...], int]] = {}
        self._published: dict[str, list[TopologySnapshot]] = {}
        self._lock = RLock()

    def create_draft(
        self,
        tenant_id: str,
        assets: tuple[Asset, ...],
        relationships: tuple[Relationship, ...],
    ) -> str:
        if any(asset.tenant_id != tenant_id for asset in assets):
            raise PermissionError("draft contains foreign tenant assets")
        validate_graph(assets, relationships)
        if self._runtime:
            with self._runtime.tenant_connection(tenant_id) as connection:
                row = connection.execute(
                    "SELECT revision FROM topology_drafts WHERE tenant_id = %s FOR UPDATE",
                    (tenant_id,),
                ).fetchone()
                revision = int(row["revision"]) + 1 if row else 1
                connection.execute(
                    """
                    INSERT INTO topology_drafts(tenant_id, revision, assets, relationships)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (tenant_id) DO UPDATE SET
                      revision = EXCLUDED.revision,
                      assets = EXCLUDED.assets,
                      relationships = EXCLUDED.relationships,
                      updated_at = now()
                    """,
                    (
                        tenant_id,
                        revision,
                        Jsonb(json.loads(json.dumps([asdict(item) for item in assets], default=str))),
                        Jsonb([asdict(item) for item in relationships]),
                    ),
                )
                return f"draft-{tenant_id}-{revision}"
        with self._lock:
            revision = self._drafts.get(tenant_id, ((), (), 0))[2] + 1
            self._drafts[tenant_id] = (assets, relationships, revision)
            return f"draft-{tenant_id}-{revision}"

    def publish(self, tenant_id: str, *, expected_draft_revision: int) -> TopologySnapshot:
        if self._runtime:
            with self._runtime.tenant_connection(tenant_id) as connection:
                draft = connection.execute(
                    "SELECT revision, assets, relationships FROM topology_drafts WHERE tenant_id = %s FOR UPDATE",
                    (tenant_id,),
                ).fetchone()
                if not draft:
                    raise ValueError("no draft exists")
                if int(draft["revision"]) != expected_draft_revision:
                    raise RuntimeError("draft changed since validation")
                assets = tuple(self._asset_from_dict(item) for item in draft["assets"])
                relationships = tuple(Relationship(**item) for item in draft["relationships"])
                validate_graph(assets, relationships)
                version_row = connection.execute(
                    "SELECT COALESCE(max(version), 0) + 1 AS version FROM topology_versions WHERE tenant_id = %s",
                    (tenant_id,),
                ).fetchone()
                version = int(version_row["version"] if version_row else 1)
                snapshot = self._snapshot(tenant_id, version, assets, relationships)
                connection.execute(
                    """
                    INSERT INTO topology_versions(
                      tenant_id, version, assets, relationships, published_at, etag
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        tenant_id,
                        version,
                        Jsonb(json.loads(json.dumps([asdict(item) for item in assets], default=str))),
                        Jsonb([asdict(item) for item in relationships]),
                        snapshot.published_at,
                        snapshot.etag,
                    ),
                )
                return snapshot
        with self._lock:
            try:
                assets, relationships, revision = self._drafts[tenant_id]
            except KeyError as error:
                raise ValueError("no draft exists") from error
            if revision != expected_draft_revision:
                raise RuntimeError("draft changed since validation")
            validate_graph(assets, relationships)
            version = len(self._published.get(tenant_id, [])) + 1
            published_at = utc_now()
            canonical = json.dumps(
                {
                    "assets": [asdict(item) for item in assets],
                    "relationships": [asdict(item) for item in relationships],
                    "version": version,
                },
                sort_keys=True,
                default=str,
            )
            snapshot = TopologySnapshot(
                version=version,
                tenant_id=tenant_id,
                assets=assets,
                relationships=relationships,
                published_at=published_at,
                etag=hashlib.sha256(canonical.encode()).hexdigest(),
            )
            self._published.setdefault(tenant_id, []).append(snapshot)
            return snapshot

    def active(self, tenant_id: str, at: datetime | None = None) -> TopologySnapshot:
        if self._runtime:
            with self._runtime.tenant_connection(tenant_id) as connection:
                if at is None:
                    row = connection.execute(
                        """
                        SELECT version, assets, relationships, published_at, etag
                        FROM topology_versions WHERE tenant_id = %s
                        ORDER BY version DESC LIMIT 1
                        """,
                        (tenant_id,),
                    ).fetchone()
                else:
                    row = connection.execute(
                        """
                        SELECT version, assets, relationships, published_at, etag
                        FROM topology_versions
                        WHERE tenant_id = %s AND published_at <= %s
                        ORDER BY version DESC LIMIT 1
                        """,
                        (tenant_id, at),
                    ).fetchone()
                if not row:
                    raise KeyError("no published topology")
                return TopologySnapshot(
                    int(row["version"]),
                    tenant_id,
                    tuple(self._asset_from_dict(item) for item in row["assets"]),
                    tuple(Relationship(**item) for item in row["relationships"]),
                    row["published_at"],
                    str(row["etag"]),
                )
        versions = self._published.get(tenant_id, [])
        if not versions:
            raise KeyError("no published topology")
        if at is None:
            return versions[-1]
        candidates = [version for version in versions if version.published_at <= at]
        if not candidates:
            raise KeyError("no topology active at timestamp")
        return candidates[-1]

    def traverse(self, tenant_id: str, root: str) -> tuple[Asset, ...]:
        snapshot = self.active(tenant_id)
        return descendants(snapshot.assets, snapshot.relationships, root)

    def versions(self, tenant_id: str) -> tuple[TopologySnapshot, ...]:
        if self._runtime:
            with self._runtime.tenant_connection(tenant_id) as connection:
                rows = connection.execute(
                    "SELECT version FROM topology_versions WHERE tenant_id = %s ORDER BY version",
                    (tenant_id,),
                ).fetchall()
            return tuple(self._version(tenant_id, int(row["version"])) for row in rows)
        return tuple(self._published.get(tenant_id, ()))

    def _version(self, tenant_id: str, version: int) -> TopologySnapshot:
        if not self._runtime:
            return self._published[tenant_id][version - 1]
        with self._runtime.tenant_connection(tenant_id) as connection:
            row = connection.execute(
                """
                SELECT assets, relationships, published_at, etag
                FROM topology_versions WHERE tenant_id = %s AND version = %s
                """,
                (tenant_id, version),
            ).fetchone()
            if not row:
                raise KeyError(version)
            return TopologySnapshot(
                version,
                tenant_id,
                tuple(self._asset_from_dict(item) for item in row["assets"]),
                tuple(Relationship(**item) for item in row["relationships"]),
                row["published_at"],
                str(row["etag"]),
            )

    @staticmethod
    def _asset_from_dict(raw: dict[str, object]) -> Asset:
        effective = raw.get("effective")
        interval = None
        if isinstance(effective, dict):
            interval = TimeInterval(
                datetime.fromisoformat(str(effective["start"])),
                datetime.fromisoformat(str(effective["end"])),
            )
        decommissioned = raw.get("decommissioned_at")
        attributes = raw.get("attributes", {})
        if not isinstance(attributes, dict):
            raise ValueError("topology asset attributes must be an object")
        return Asset(
            id=str(raw["id"]),
            tenant_id=str(raw["tenant_id"]),
            site_id=str(raw["site_id"]),
            kind=AssetType(str(raw["kind"])),
            name=str(raw["name"]),
            capacity_kw=Decimal(str(raw.get("capacity_kw", 0))),
            attributes={str(key): str(value) for key, value in attributes.items()},
            effective=interval,
            decommissioned_at=datetime.fromisoformat(str(decommissioned)) if decommissioned else None,
        )

    @staticmethod
    def _snapshot(
        tenant_id: str,
        version: int,
        assets: tuple[Asset, ...],
        relationships: tuple[Relationship, ...],
    ) -> TopologySnapshot:
        published_at = utc_now()
        canonical = json.dumps(
            {
                "assets": [asdict(item) for item in assets],
                "relationships": [asdict(item) for item in relationships],
                "version": version,
            },
            sort_keys=True,
            default=str,
        )
        return TopologySnapshot(
            version,
            tenant_id,
            assets,
            relationships,
            published_at,
            hashlib.sha256(canonical.encode()).hexdigest(),
        )
