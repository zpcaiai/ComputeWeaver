from __future__ import annotations

from packages.scheduling.contracts import ScheduleInput


def diagnose_infeasibility(request: ScheduleInput) -> tuple[str, ...]:
    issues: list[str] = []
    if not request.slots:
        issues.append("NO_TIME_SLOTS")
    max_gpus = max((slot.gpu_capacity for slot in request.slots), default=0)
    for job in request.jobs:
        if job.request.gpu_count > max_gpus:
            issues.append(f"GPU_CAPACITY:{job.id}")
        if not any(slot.starts_at < job.sla.deadline for slot in request.slots):
            issues.append(f"DEADLINE:{job.id}")
    return tuple(issues)
