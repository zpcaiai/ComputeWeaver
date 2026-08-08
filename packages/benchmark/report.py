from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from packages.scheduling.contracts import SchedulePlan


@dataclass(frozen=True, slots=True)
class StrategyComparison:
    baseline: str
    candidate: str
    cost_savings: Decimal
    energy_savings_kwh: Decimal
    scheduled_job_delta: int
    violation_delta: int


def compare_strategies(baseline: SchedulePlan, candidate: SchedulePlan) -> StrategyComparison:
    baseline_jobs = {item.job_id for item in baseline.allocations}
    candidate_jobs = {item.job_id for item in candidate.allocations}
    return StrategyComparison(
        baseline.strategy,
        candidate.strategy,
        baseline.estimated_cost - candidate.estimated_cost,
        baseline.energy_intent_kwh - candidate.energy_intent_kwh,
        len(candidate_jobs) - len(baseline_jobs),
        len(candidate.violations) - len(baseline.violations),
    )
