from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from packages.workloads.models import Job

from .contracts import Allocation, ScheduleInput, SchedulePlan, validate_plan


def _schedule(
    request: ScheduleInput, ordered_jobs: tuple[Job, ...], strategy: str, price: bool = False
) -> SchedulePlan:
    available = {slot.index: slot.gpu_capacity for slot in request.slots}
    allocations: list[Allocation] = []
    unscheduled: list[str] = []
    cost = Decimal(0)
    energy = Decimal(0)
    for job in ordered_jobs:
        if not request.slots:
            unscheduled.append(job.id)
            continue
        duration = request.slots[0].duration_hours
        required = int((job.request.estimated_hours / duration).to_integral_value(rounding=ROUND_CEILING))
        candidates = [
            slot
            for slot in request.slots
            if slot.starts_at < job.sla.deadline
            and available[slot.index] >= job.request.gpu_count
            and job.request.power_kw_per_gpu * job.request.gpu_count <= slot.power_capacity_kw
        ]
        if price:
            candidates.sort(key=lambda item: (item.price_per_kwh, item.starts_at))
        else:
            candidates.sort(key=lambda item: item.starts_at)
        selected = sorted(candidates[:required], key=lambda item: item.starts_at)
        consecutive = all(right.index == left.index + 1 for left, right in zip(selected, selected[1:], strict=False))
        if len(selected) != required or (not job.checkpointable and not consecutive):
            unscheduled.append(job.id)
            continue
        for slot in selected:
            available[slot.index] -= job.request.gpu_count
            slot_energy = job.request.power_kw_per_gpu * job.request.gpu_count * slot.duration_hours
            energy += slot_energy
            cost += slot_energy * slot.price_per_kwh
        allocations.append(
            Allocation(
                job.id,
                tuple(item.index for item in selected),
                job.request.gpu_count,
                strategy.upper(),
            )
        )
    plan = SchedulePlan(
        strategy=strategy,
        allocations=tuple(allocations),
        unscheduled=tuple(unscheduled),
        energy_intent_kwh=energy,
        estimated_cost=cost,
        assumptions=("deterministic input snapshot",),
        violations=(),
        input_hash=request.content_hash(),
    )
    validate_plan(request, plan)
    return plan


def schedule_fifo(request: ScheduleInput) -> SchedulePlan:
    return _schedule(request, tuple(sorted(request.jobs, key=lambda job: (job.submitted_at, job.id))), "fifo")


def schedule_priority_edf(request: ScheduleInput) -> SchedulePlan:
    ordered = tuple(sorted(request.jobs, key=lambda job: (-job.sla.priority, job.sla.deadline, job.id)))
    return _schedule(request, ordered, "priority_edf")


def schedule_price_aware(request: ScheduleInput) -> SchedulePlan:
    ordered = tuple(sorted(request.jobs, key=lambda job: (-job.sla.priority, job.sla.deadline, job.id)))
    return _schedule(request, ordered, "price_aware", price=True)
