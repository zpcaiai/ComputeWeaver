from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from packages.scheduling.contracts import SchedulePlan


@dataclass(frozen=True, slots=True)
class PlanDiff:
    added_jobs: frozenset[str]
    removed_jobs: frozenset[str]
    changed_jobs: frozenset[str]
    cost_delta: Decimal
    energy_delta_kwh: Decimal


def compare(left: SchedulePlan, right: SchedulePlan) -> PlanDiff:
    left_map = {item.job_id: item.slot_indices for item in left.allocations}
    right_map = {item.job_id: item.slot_indices for item in right.allocations}
    shared = left_map.keys() & right_map.keys()
    return PlanDiff(
        frozenset(right_map.keys() - left_map.keys()),
        frozenset(left_map.keys() - right_map.keys()),
        frozenset(item for item in shared if left_map[item] != right_map[item]),
        right.estimated_cost - left.estimated_cost,
        right.energy_intent_kwh - left.energy_intent_kwh,
    )
