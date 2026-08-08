from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from packages.scheduling.contracts import ScheduleInput


@dataclass(frozen=True, slots=True)
class ObjectiveWeights:
    energy_cost: Decimal = Decimal(1)
    carbon: Decimal = Decimal(0)
    delay: Decimal = Decimal(0)
    migration: Decimal = Decimal(0)
    degradation: Decimal = Decimal(0)
    curtailment: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        values = (
            self.energy_cost,
            self.carbon,
            self.delay,
            self.migration,
            self.degradation,
            self.curtailment,
        )
        if any(value < 0 for value in values):
            raise ValueError("objective weights cannot be negative")
        if not any(values):
            raise ValueError("at least one objective weight must be positive")


@dataclass(frozen=True, slots=True)
class ModelDimensions:
    jobs: tuple[str, ...]
    slots: tuple[int, ...]
    sites: tuple[str, ...]
    gpu_models: tuple[str, ...]


def build_dimensions(request: ScheduleInput) -> ModelDimensions:
    slots = tuple(item.index for item in request.slots)
    if len(set(slots)) != len(slots):
        raise ValueError("optimization slot indices must be unique")
    jobs = tuple(item.id for item in request.jobs)
    if len(set(jobs)) != len(jobs):
        raise ValueError("optimization job IDs must be unique")
    return ModelDimensions(
        jobs,
        slots,
        tuple(sorted({site for job in request.jobs for site in job.allowed_sites})),
        tuple(sorted({job.request.gpu_model or "any" for job in request.jobs})),
    )
