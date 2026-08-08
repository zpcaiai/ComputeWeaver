from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ObservedState:
    completed_jobs: frozenset[str]
    running_progress: dict[str, Decimal]
    available_gpus: int
    battery_soc: Decimal
    grid_limit_kw: Decimal
    data_quality: Decimal
    version: str


@dataclass(frozen=True, slots=True)
class StateDiff:
    removed_jobs: frozenset[str]
    progress_errors: dict[str, Decimal]
    material: bool


def reconcile(planned_progress: dict[str, Decimal], observed: ObservedState) -> StateDiff:
    removed = frozenset(planned_progress) - frozenset(observed.running_progress) - observed.completed_jobs
    errors = {
        job_id: observed.running_progress.get(job_id, Decimal(1)) - planned
        for job_id, planned in planned_progress.items()
        if job_id in observed.running_progress and abs(observed.running_progress[job_id] - planned) > Decimal("0.01")
    }
    return StateDiff(removed, errors, bool(removed or errors))
