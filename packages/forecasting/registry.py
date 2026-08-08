from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from packages.persistence.postgres import PostgresRuntime


class ModelStage(StrEnum):
    REGISTERED = "registered"
    STAGING = "staging"
    PRODUCTION = "production"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class ModelVersion:
    name: str
    version: str
    artifact_hash: str
    dataset_hash: str
    created_at: datetime
    stage: ModelStage = ModelStage.REGISTERED


class ModelRegistry:
    def __init__(self, runtime: PostgresRuntime | None = None) -> None:
        self._runtime = runtime
        self._models: dict[tuple[str, str], ModelVersion] = {}
        self._history: dict[str, list[str]] = {}

    @staticmethod
    def _from_row(row: dict[str, object]) -> ModelVersion:
        return ModelVersion(
            str(row["name"]),
            str(row["version"]),
            str(row["artifact_hash"]),
            str(row["dataset_hash"]),
            row["created_at"],  # type: ignore[arg-type]
            ModelStage(str(row["stage"])),
        )

    def register(self, model: ModelVersion, *, tenant_id: str | None = None) -> None:
        if self._runtime:
            if not tenant_id:
                raise ValueError("tenant_id is required for persistent model registry")
            with self._runtime.tenant_connection(tenant_id) as connection:
                existing = connection.execute(
                    "SELECT 1 FROM model_versions WHERE tenant_id = %s AND name = %s AND version = %s",
                    (tenant_id, model.name, model.version),
                ).fetchone()
                if existing:
                    raise ValueError("model version already exists")
                connection.execute(
                    """
                    INSERT INTO model_versions(
                      tenant_id, name, version, artifact_hash, dataset_hash, stage, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        tenant_id,
                        model.name,
                        model.version,
                        model.artifact_hash,
                        model.dataset_hash,
                        model.stage.value,
                        model.created_at,
                    ),
                )
            return
        key = (model.name, model.version)
        if key in self._models:
            raise ValueError("model version already exists")
        self._models[key] = model

    def promote(self, name: str, version: str, *, tenant_id: str | None = None) -> ModelVersion:
        if self._runtime:
            if not tenant_id:
                raise ValueError("tenant_id is required for persistent model registry")
            with self._runtime.tenant_connection(tenant_id) as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 29))",
                    (f"{tenant_id}:{name}",),
                )
                target = connection.execute(
                    """
                    SELECT * FROM model_versions
                    WHERE tenant_id = %s AND name = %s AND version = %s
                    FOR UPDATE
                    """,
                    (tenant_id, name, version),
                ).fetchone()
                if not target:
                    raise KeyError((name, version))
                connection.execute(
                    """
                    UPDATE model_versions SET stage = 'archived', updated_at = now()
                    WHERE tenant_id = %s AND name = %s AND stage = 'production'
                    """,
                    (tenant_id, name),
                )
                row = connection.execute(
                    """
                    UPDATE model_versions SET stage = 'production', updated_at = now()
                    WHERE tenant_id = %s AND name = %s AND version = %s
                    RETURNING *
                    """,
                    (tenant_id, name, version),
                ).fetchone()
                if not row:
                    raise RuntimeError("model promotion returned no model")
                return self._from_row(row)
        key = (name, version)
        model = self._models[key]
        for existing_key, existing in tuple(self._models.items()):
            if existing.name == name and existing.stage == ModelStage.PRODUCTION:
                self._models[existing_key] = ModelVersion(
                    existing.name,
                    existing.version,
                    existing.artifact_hash,
                    existing.dataset_hash,
                    existing.created_at,
                    ModelStage.ARCHIVED,
                )
        promoted = ModelVersion(
            model.name,
            model.version,
            model.artifact_hash,
            model.dataset_hash,
            model.created_at,
            ModelStage.PRODUCTION,
        )
        self._models[key] = promoted
        self._history.setdefault(name, []).append(version)
        return promoted

    def rollback(self, name: str, *, tenant_id: str | None = None) -> ModelVersion:
        if self._runtime:
            if not tenant_id:
                raise ValueError("tenant_id is required for persistent model registry")
            with self._runtime.tenant_connection(tenant_id) as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 29))",
                    (f"{tenant_id}:{name}",),
                )
                row = connection.execute(
                    """
                    SELECT version FROM model_versions
                    WHERE tenant_id = %s AND name = %s AND stage = 'archived'
                    ORDER BY updated_at DESC LIMIT 1
                    FOR UPDATE
                    """,
                    (tenant_id, name),
                ).fetchone()
                if not row:
                    raise ValueError("no previous production model")
                version = str(row["version"])
                target = connection.execute(
                    """
                    SELECT * FROM model_versions
                    WHERE tenant_id = %s AND name = %s AND version = %s
                    FOR UPDATE
                    """,
                    (tenant_id, name, version),
                ).fetchone()
                if not target:
                    raise KeyError((name, version))
                connection.execute(
                    """
                    UPDATE model_versions SET stage = 'archived', updated_at = now()
                    WHERE tenant_id = %s AND name = %s AND stage = 'production'
                    """,
                    (tenant_id, name),
                )
                promoted = connection.execute(
                    """
                    UPDATE model_versions SET stage = 'production', updated_at = now()
                    WHERE tenant_id = %s AND name = %s AND version = %s
                    RETURNING *
                    """,
                    (tenant_id, name, version),
                ).fetchone()
                if not promoted:
                    raise RuntimeError("model rollback returned no model")
                return self._from_row(promoted)
        history = self._history.get(name, [])
        if len(history) < 2:
            raise ValueError("no previous production model")
        history.pop()
        return self.promote(name, history[-1])

    def production(self, name: str, *, tenant_id: str | None = None) -> ModelVersion:
        if self._runtime:
            if not tenant_id:
                raise ValueError("tenant_id is required for persistent model registry")
            with self._runtime.tenant_connection(tenant_id) as connection:
                row = connection.execute(
                    """
                    SELECT * FROM model_versions
                    WHERE tenant_id = %s AND name = %s AND stage = 'production'
                    """,
                    (tenant_id, name),
                ).fetchone()
                if not row:
                    raise KeyError(name)
                return self._from_row(row)
        for model in self._models.values():
            if model.name == name and model.stage == ModelStage.PRODUCTION:
                return model
        raise KeyError(name)
