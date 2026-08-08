from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from packages.scheduling.contracts import ScheduleInput, SchedulePlan

from .model import ObjectiveWeights


@dataclass(frozen=True, slots=True)
class ObjectiveBreakdown:
    energy_cost: Decimal
    carbon_kg: Decimal
    delay_slot_hours: Decimal
    weighted_total: Decimal


def evaluate_objectives(
    request: ScheduleInput,
    plan: SchedulePlan,
    weights: ObjectiveWeights = ObjectiveWeights(),
) -> ObjectiveBreakdown:
    slots = {slot.index: slot for slot in request.slots}
    jobs = {job.id: job for job in request.jobs}
    energy_cost = Decimal(0)
    carbon = Decimal(0)
    delay = Decimal(0)
    for allocation in plan.allocations:
        job = jobs[allocation.job_id]
        for slot_index in allocation.slot_indices:
            slot = slots[slot_index]
            energy = slot.duration_hours * Decimal(allocation.gpu_count) * job.request.power_kw_per_gpu
            energy_cost += energy * slot.price_per_kwh
            carbon += energy * slot.carbon_kg_per_kwh
            delay += max(Decimal(0), Decimal(str((slot.starts_at - job.submitted_at).total_seconds() / 3600)))
    total = energy_cost * weights.energy_cost + carbon * weights.carbon + delay * weights.delay
    return ObjectiveBreakdown(energy_cost, carbon, delay, total)
