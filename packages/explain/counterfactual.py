from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from packages.optimization.engine import OptimizationResult, optimize
from packages.scheduling.contracts import ScheduleInput


@dataclass(frozen=True, slots=True)
class Counterfactual:
    parameter: str
    original: Decimal
    changed: Decimal
    objective_delta: Decimal | None
    result: OptimizationResult


def change_slot_prices(request: ScheduleInput, multiplier: Decimal, timeout_seconds: float = 5) -> Counterfactual:
    if multiplier <= 0:
        raise ValueError("price multiplier must be positive")
    original_result = optimize(request, timeout_seconds)
    changed_slots = tuple(replace(slot, price_per_kwh=slot.price_per_kwh * multiplier) for slot in request.slots)
    changed_request = replace(request, slots=changed_slots)
    changed_result = optimize(changed_request, timeout_seconds)
    delta = None
    if original_result.plan and changed_result.plan:
        delta = changed_result.plan.estimated_cost - original_result.plan.estimated_cost
    return Counterfactual("price_multiplier", Decimal(1), multiplier, delta, changed_result)
