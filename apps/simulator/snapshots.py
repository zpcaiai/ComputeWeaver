from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SnapshotEnvelope:
    schema_version: str
    state: dict[str, Any]
    sha256: str


def create_snapshot(state: dict[str, Any]) -> SnapshotEnvelope:
    normalized = json.loads(json.dumps(state, default=str, sort_keys=True))
    payload = json.dumps(normalized, separators=(",", ":"), sort_keys=True).encode()
    return SnapshotEnvelope("1.0", normalized, hashlib.sha256(payload).hexdigest())


def verify_snapshot(snapshot: SnapshotEnvelope) -> bool:
    payload = json.dumps(snapshot.state, separators=(",", ":"), sort_keys=True).encode()
    return snapshot.schema_version == "1.0" and hashlib.sha256(payload).hexdigest() == snapshot.sha256
