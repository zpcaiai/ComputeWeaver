from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from packages.optimization.engine import OptimizationResult, optimize
from packages.scheduling.contracts import ScheduleInput


@dataclass(frozen=True, slots=True)
class WhatIfResult:
    isolated: bool
    changed_parameters: dict[str, str]
    result: OptimizationResult


def run_what_if(request: ScheduleInput, *, capacity_multiplier: Decimal = Decimal(1)) -> WhatIfResult:
    if capacity_multiplier <= 0:
        raise ValueError("capacity multiplier must be positive")
    slots = tuple(
        replace(
            slot,
            gpu_capacity=int(Decimal(slot.gpu_capacity) * capacity_multiplier),
            power_capacity_kw=slot.power_capacity_kw * capacity_multiplier,
        )
        for slot in request.slots
    )
    scenario = replace(request, slots=slots)
    return WhatIfResult(
        True,
        {"capacity_multiplier": str(capacity_multiplier)},
        optimize(scenario),
    )
