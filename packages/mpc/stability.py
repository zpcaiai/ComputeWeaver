from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from packages.scheduling.contracts import SchedulePlan


@dataclass(frozen=True, slots=True)
class StabilityScore:
    changed_jobs: int
    total_jobs: int
    churn: Decimal
    acceptable: bool


def score_stability(
    previous: SchedulePlan,
    candidate: SchedulePlan,
    *,
    maximum_churn: Decimal = Decimal("0.5"),
) -> StabilityScore:
    if not 0 <= maximum_churn <= 1:
        raise ValueError("maximum churn must be in [0,1]")
    before = {item.job_id: item.slot_indices for item in previous.allocations}
    after = {item.job_id: item.slot_indices for item in candidate.allocations}
    subjects = before.keys() | after.keys()
    changed = sum(before.get(job) != after.get(job) for job in subjects)
    churn = Decimal(changed) / Decimal(max(1, len(subjects)))
    return StabilityScore(changed, len(subjects), churn, churn <= maximum_churn)
