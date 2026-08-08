from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from packages.scheduling.contracts import Allocation, ScheduleInput, SchedulePlan, validate_plan


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    status: str
    plan: SchedulePlan | None
    objective_breakdown: dict[str, Decimal]
    solver: str
    gap: Decimal
    runtime_seconds: float
    assumptions: tuple[str, ...]
    diagnostics: tuple[str, ...]


def _job_options(request: ScheduleInput, job_index: int) -> list[tuple[int, ...]]:
    job = request.jobs[job_index]
    if not request.slots:
        return []
    needed = int(
        (job.request.estimated_hours / request.slots[0].duration_hours).to_integral_value(rounding=ROUND_CEILING)
    )
    eligible = [
        slot.index
        for slot in request.slots
        if slot.starts_at < job.sla.deadline
        and job.request.gpu_count <= slot.gpu_capacity
        and job.request.gpu_count * job.request.power_kw_per_gpu <= slot.power_capacity_kw
    ]
    options: list[tuple[int, ...]] = []
    for choice in itertools.combinations(eligible, needed):
        if job.checkpointable or all(b == a + 1 for a, b in zip(choice, choice[1:], strict=False)):
            options.append(choice)
    return options


def optimize(request: ScheduleInput, timeout_seconds: float = 10.0) -> OptimizationResult:
    """Exact finite time-indexed binary optimizer for the bounded MVP problem.

    It enumerates feasible per-job placement choices with branch pruning. Unlike a heuristic,
    a completed search proves the returned objective is globally optimal for this model.
    """
    started = time.perf_counter()
    options = [_job_options(request, index) for index in range(len(request.jobs))]
    empty = [request.jobs[index].id for index, choices in enumerate(options) if not choices]
    if empty:
        return OptimizationResult(
            "infeasible",
            None,
            {},
            "exact-enumeration",
            Decimal(0),
            time.perf_counter() - started,
            (),
            tuple(f"JOB_NO_FEASIBLE_WINDOW:{job_id}" for job_id in empty),
        )
    capacity = {slot.index: slot.gpu_capacity for slot in request.slots}
    best_cost: Decimal | None = None
    best_choices: list[tuple[int, ...]] | None = None
    current: list[tuple[int, ...]] = []
    timed_out = False

    def search(index: int, running_cost: Decimal) -> None:
        nonlocal best_cost, best_choices, timed_out
        if time.perf_counter() - started > timeout_seconds:
            timed_out = True
            return
        if best_cost is not None and running_cost >= best_cost:
            return
        if index == len(request.jobs):
            best_cost = running_cost
            best_choices = list(current)
            return
        job = request.jobs[index]
        for choice in options[index]:
            if all(capacity[slot] >= job.request.gpu_count for slot in choice):
                for slot in choice:
                    capacity[slot] -= job.request.gpu_count
                current.append(choice)
                cost = sum(
                    (
                        request.slots[slot].duration_hours
                        * job.request.gpu_count
                        * job.request.power_kw_per_gpu
                        * request.slots[slot].price_per_kwh
                        for slot in choice
                    ),
                    Decimal(0),
                )
                search(index + 1, running_cost + cost)
                current.pop()
                for slot in choice:
                    capacity[slot] += job.request.gpu_count

    search(0, Decimal(0))
    runtime = time.perf_counter() - started
    if best_choices is None:
        return OptimizationResult(
            "timeout" if timed_out else "infeasible",
            None,
            {},
            "exact-enumeration",
            Decimal(1) if timed_out else Decimal(0),
            runtime,
            (),
            ("SEARCH_TIMEOUT",) if timed_out else ("GPU_CAPACITY_CONFLICT",),
        )
    allocations = tuple(
        Allocation(job.id, best_choices[index], job.request.gpu_count, "MIN_TOTAL_COST")
        for index, job in enumerate(request.jobs)
    )
    energy = sum(
        (
            request.slots[slot_index].duration_hours * job.request.gpu_count * job.request.power_kw_per_gpu
            for index, job in enumerate(request.jobs)
            for slot_index in best_choices[index]
        ),
        Decimal(0),
    )
    plan = SchedulePlan(
        strategy="exact_co_optimizer",
        allocations=allocations,
        unscheduled=(),
        energy_intent_kwh=energy,
        estimated_cost=best_cost or Decimal(0),
        assumptions=("perfect slot forecast", "single-site bounded MVP"),
        violations=(),
        input_hash=request.content_hash(),
    )
    validate_plan(request, plan)
    return OptimizationResult(
        "feasible_timeout" if timed_out else "optimal",
        plan,
        {"energy_cost": plan.estimated_cost, "delay": Decimal(0)},
        "exact-enumeration",
        Decimal(0) if not timed_out else Decimal(1),
        runtime,
        plan.assumptions,
        (),
    )
