from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class CriticalLoad:
    id: str
    power_kw: Decimal
    priority: int
    critical: bool


@dataclass(frozen=True, slots=True)
class DegradedPlan:
    served: tuple[str, ...]
    shed: tuple[str, ...]
    used_power_kw: Decimal
    hard_violations: tuple[str, ...]


def plan_degraded_mode(loads: tuple[CriticalLoad, ...], available_power_kw: Decimal) -> DegradedPlan:
    served: list[str] = []
    shed: list[str] = []
    used = Decimal(0)
    ordered = sorted(loads, key=lambda item: (not item.critical, -item.priority, item.id))
    for load in ordered:
        if used + load.power_kw <= available_power_kw:
            served.append(load.id)
            used += load.power_kw
        else:
            shed.append(load.id)
    violations = tuple(f"CRITICAL_LOAD_SHED:{load.id}" for load in loads if load.critical and load.id in shed)
    return DegradedPlan(tuple(served), tuple(shed), used, violations)
