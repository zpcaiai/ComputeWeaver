from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ConfigVersion:
    version: int
    values: dict[str, object]
    actor_id: str
    created_at: datetime
    content_hash: str


class VersionedConfig:
    def __init__(self, allowed_keys: frozenset[str]) -> None:
        self.allowed_keys = allowed_keys
        self._versions: list[ConfigVersion] = []

    def update(self, values: dict[str, object], actor_id: str, now: datetime) -> ConfigVersion:
        unknown = values.keys() - self.allowed_keys
        if unknown:
            raise ValueError(f"unknown configuration keys {sorted(unknown)}")
        current = dict(self._versions[-1].values) if self._versions else {}
        current.update(values)
        digest = hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()
        version = ConfigVersion(len(self._versions) + 1, current, actor_id, now, digest)
        self._versions.append(version)
        return version

    def rollback(self, target_version: int, actor_id: str, now: datetime) -> ConfigVersion:
        if not 1 <= target_version <= len(self._versions):
            raise ValueError("unknown configuration version")
        return self.update(dict(self._versions[target_version - 1].values), actor_id, now)

    @property
    def current(self) -> ConfigVersion:
        return self._versions[-1]
