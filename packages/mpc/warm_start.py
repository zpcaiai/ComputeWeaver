from __future__ import annotations

from packages.scheduling.contracts import Allocation, ScheduleInput, SchedulePlan


def reusable_allocations(previous: SchedulePlan, request: ScheduleInput) -> tuple[Allocation, ...]:
    valid_jobs = {job.id for job in request.jobs}
    valid_slots = {slot.index for slot in request.slots}
    return tuple(
        allocation
        for allocation in previous.allocations
        if allocation.job_id in valid_jobs and set(allocation.slot_indices) <= valid_slots
    )
