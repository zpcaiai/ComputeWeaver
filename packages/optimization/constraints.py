from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from packages.scheduling.contracts import ScheduleInput


@dataclass(frozen=True, slots=True)
class ConstraintViolation:
    constraint_id: str
    subject_id: str
    detail: str


def validate_hard_constraints(request: ScheduleInput) -> tuple[ConstraintViolation, ...]:
    violations: list[ConstraintViolation] = []
    for job in request.jobs:
        eligible = [slot for slot in request.slots if slot.starts_at < job.sla.deadline]
        if not eligible:
            violations.append(ConstraintViolation("SLA_DEADLINE", job.id, "no slot precedes deadline"))
            continue
        if all(slot.gpu_capacity < job.request.gpu_count for slot in eligible):
            violations.append(ConstraintViolation("GPU_CAPACITY", job.id, "insufficient GPUs"))
        required_power = Decimal(job.request.gpu_count) * job.request.power_kw_per_gpu
        if all(slot.power_capacity_kw < required_power for slot in eligible):
            violations.append(ConstraintViolation("POWER_CAPACITY", job.id, "insufficient site power"))
        if not job.allowed_sites:
            violations.append(ConstraintViolation("SOVEREIGNTY_SCOPE", job.id, "no allowed site"))
    return tuple(violations)
