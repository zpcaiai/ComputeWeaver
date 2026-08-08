from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any


class HealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE = "stale"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class SourceObservation:
    source: str
    observed_at: datetime
    values: dict[str, Any]
    priority: int = 0


@dataclass(frozen=True, slots=True)
class ReconciledHealth:
    state: HealthState
    selected_source: str | None
    values: dict[str, Any]
    stale_sources: tuple[str, ...]
    conflicts: tuple[str, ...]


def reconcile_health(
    observations: tuple[SourceObservation, ...],
    *,
    now: datetime,
    freshness: timedelta,
    conflict_fields: frozenset[str] = frozenset({"online", "gpu_count"}),
) -> ReconciledHealth:
    if freshness <= timedelta(0):
        raise ValueError("freshness threshold must be positive")
    if any(item.observed_at.tzinfo is None for item in observations) or now.tzinfo is None:
        raise ValueError("health timestamps must be timezone-aware")
    fresh = tuple(item for item in observations if now - item.observed_at <= freshness)
    stale = tuple(sorted(item.source for item in observations if item not in fresh))
    if not fresh:
        return ReconciledHealth(HealthState.STALE, None, {}, stale, ())
    selected = max(fresh, key=lambda item: (item.priority, item.observed_at, item.source))
    conflicts = tuple(
        sorted(
            field
            for field in conflict_fields
            if len({repr(item.values.get(field)) for item in fresh if field in item.values}) > 1
        )
    )
    state = HealthState.CONFLICT if conflicts else (HealthState.DEGRADED if stale else HealthState.HEALTHY)
    return ReconciledHealth(state, selected.source, dict(selected.values), stale, conflicts)
