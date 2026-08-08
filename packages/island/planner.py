from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from packages.resilience.planner import CriticalLoad, plan_degraded_mode


@dataclass(frozen=True, slots=True)
class IslandPlan:
    served: tuple[str, ...]
    shed: tuple[str, ...]
    survival_hours: Decimal
    reserve_kwh: Decimal
    violations: tuple[str, ...]


def plan_island_survival(
    loads: tuple[CriticalLoad, ...],
    *,
    battery_kwh: Decimal,
    reserved_kwh: Decimal,
    generator_kwh: Decimal,
    pv_kw: Decimal,
) -> IslandPlan:
    if not 0 <= reserved_kwh <= battery_kwh:
        raise ValueError("invalid island reserve")
    usable = battery_kwh - reserved_kwh + generator_kwh
    available_power = pv_kw + usable
    degraded = plan_degraded_mode(loads, available_power)
    net_load = max(Decimal("0.001"), degraded.used_power_kw - pv_kw)
    survival = usable / net_load
    return IslandPlan(
        degraded.served,
        degraded.shed,
        survival,
        reserved_kwh,
        degraded.hard_violations,
    )
