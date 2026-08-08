from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class Evaluation:
    energy_cost: Decimal
    peak_grid_kw: Decimal
    sla_violations: int
    average_gpu_utilization: Decimal
    renewable_fraction: Decimal
    hard_violations: int

    def as_dict(self) -> dict[str, Any]:
        return {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(self).items()}


def evaluate_events(events: list[dict[str, Any]]) -> Evaluation:
    if not events:
        return Evaluation(Decimal(0), Decimal(0), 0, Decimal(0), Decimal(0), 0)
    grid = [Decimal(str(item["grid_kw"])) for item in events]
    pv = [Decimal(str(item["pv_kw"])) for item in events]
    facility = [Decimal(str(item["facility_kw"])) for item in events]
    compute = [Decimal(str(item["compute_kw"])) for item in events]
    interval_hours = Decimal("0.25")
    cost = sum(
        (
            Decimal(str(item["grid_kw"])) * interval_hours * Decimal(str(item.get("price_per_kwh", "0.1")))
            for item in events
        ),
        Decimal(0),
    )
    total_energy = sum(facility, Decimal(0)) * interval_hours
    renewable = min(Decimal(1), sum(pv, Decimal(0)) * interval_hours / max(total_energy, Decimal("0.001")))
    utilization = sum(compute, Decimal(0)) / (Decimal(len(events)) * Decimal("9.6"))
    hard = sum(
        1
        for item in events
        if Decimal(str(item["battery_soc"])) < Decimal("0.1")
        or Decimal(str(item["battery_soc"])) > Decimal("0.9")
        or Decimal(str(item.get("unserved_kw", 0))) > Decimal("0.001")
        or (
            item.get("grid_limit_kw") is not None
            and Decimal(str(item["grid_kw"])) > Decimal(str(item["grid_limit_kw"]))
        )
    )
    return Evaluation(cost, max(grid), 0, min(Decimal(1), utilization), renewable, hard)
