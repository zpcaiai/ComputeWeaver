from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal

from packages.workloads.models import Job


@dataclass(frozen=True, slots=True)
class TimeSlot:
    index: int
    starts_at: datetime
    duration_hours: Decimal
    gpu_capacity: int
    power_capacity_kw: Decimal
    price_per_kwh: Decimal
    carbon_kg_per_kwh: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class ScheduleInput:
    jobs: tuple[Job, ...]
    slots: tuple[TimeSlot, ...]
    topology_version: int
    forecast_version: str
    baseline_name: str = "none"

    def content_hash(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), default=str, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Allocation:
    job_id: str
    slot_indices: tuple[int, ...]
    gpu_count: int
    reason_code: str


@dataclass(frozen=True, slots=True)
class SchedulePlan:
    strategy: str
    allocations: tuple[Allocation, ...]
    unscheduled: tuple[str, ...]
    energy_intent_kwh: Decimal
    estimated_cost: Decimal
    assumptions: tuple[str, ...]
    violations: tuple[str, ...]
    input_hash: str


def validate_plan(request: ScheduleInput, plan: SchedulePlan) -> None:
    used = {slot.index: 0 for slot in request.slots}
    jobs = {job.id: job for job in request.jobs}
    allocated: set[str] = set()
    for allocation in plan.allocations:
        if allocation.job_id in allocated:
            raise ValueError("job allocated more than once")
        allocated.add(allocation.job_id)
        job = jobs[allocation.job_id]
        for slot_index in allocation.slot_indices:
            slot = request.slots[slot_index]
            used[slot_index] += allocation.gpu_count
            if used[slot_index] > slot.gpu_capacity:
                raise ValueError("schedule exceeds GPU capacity")
            if slot.starts_at >= job.sla.deadline:
                raise ValueError("schedule violates job deadline")
