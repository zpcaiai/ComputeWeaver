from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class EnergyStep:
    facility_kw: Decimal
    pv_kw: Decimal
    battery_kw: Decimal
    grid_kw: Decimal
    unserved_kw: Decimal
    battery_soc: Decimal


class SimulatedEnergySystem:
    def __init__(self, *, battery_capacity_kwh: Decimal, initial_soc: Decimal = Decimal("0.5")) -> None:
        if battery_capacity_kwh <= 0 or not 0 <= initial_soc <= 1:
            raise ValueError("invalid simulated battery configuration")
        self.capacity = battery_capacity_kwh
        self.soc = initial_soc

    def step(
        self,
        *,
        facility_kw: Decimal,
        pv_kw: Decimal,
        grid_limit_kw: Decimal,
        duration_hours: Decimal,
    ) -> EnergyStep:
        if min(facility_kw, pv_kw, grid_limit_kw, duration_hours) < 0 or duration_hours == 0:
            raise ValueError("invalid simulated energy interval")
        net = facility_kw - pv_kw
        available_discharge = max(Decimal(0), (self.soc - Decimal("0.1")) * self.capacity / duration_hours)
        discharge = min(max(net - grid_limit_kw, Decimal(0)), available_discharge)
        grid = min(max(net - discharge, Decimal(0)), grid_limit_kw)
        unserved = max(net - discharge - grid, Decimal(0))
        self.soc = max(Decimal("0.1"), self.soc - discharge * duration_hours / self.capacity)
        return EnergyStep(facility_kw, pv_kw, -discharge, grid, unserved, self.soc)
