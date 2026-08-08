from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Backup:
    created_at: datetime
    state: dict[str, Any]
    sha256: str


@dataclass(frozen=True, slots=True)
class Reconciliation:
    missing_external: tuple[str, ...]
    unexpected_external: tuple[str, ...]
    mismatched: tuple[str, ...]
    safe: bool


def backup_state(state: dict[str, Any], now: datetime) -> Backup:
    payload = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return Backup(now, json.loads(payload), hashlib.sha256(payload.encode()).hexdigest())


def verify_backup(backup: Backup) -> bool:
    payload = json.dumps(backup.state, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest() == backup.sha256


def reconcile_restore(planned: dict[str, Any], actual: dict[str, Any]) -> Reconciliation:
    missing = tuple(sorted(planned.keys() - actual.keys()))
    unexpected = tuple(sorted(actual.keys() - planned.keys()))
    mismatched = tuple(sorted(key for key in planned.keys() & actual.keys() if planned[key] != actual[key]))
    return Reconciliation(missing, unexpected, mismatched, not (missing or mismatched))
