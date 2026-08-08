from __future__ import annotations

from dataclasses import dataclass

from packages.scheduling.contracts import ScheduleInput, SchedulePlan
from packages.scheduling.strategies import (
    schedule_fifo,
    schedule_price_aware,
    schedule_priority_edf,
)


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    seed: int
    input_hash: str
    plans: tuple[SchedulePlan, ...]
    cheapest: str


def benchmark(request: ScheduleInput, seed: int) -> BenchmarkResult:
    plans = (
        schedule_fifo(request),
        schedule_priority_edf(request),
        schedule_price_aware(request),
    )
    input_hashes = {plan.input_hash for plan in plans}
    if len(input_hashes) != 1:
        raise RuntimeError("benchmark strategies used different inputs")
    cheapest = min(plans, key=lambda plan: (plan.estimated_cost, plan.strategy)).strategy
    return BenchmarkResult(seed, plans[0].input_hash, plans, cheapest)
