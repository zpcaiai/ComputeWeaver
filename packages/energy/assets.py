from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class GridConnection:
    id: str
    topology_asset_id: str
    import_limit_kw: Decimal
    export_limit_kw: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class Photovoltaic:
    id: str
    topology_asset_id: str
    capacity_kw: Decimal


@dataclass(frozen=True, slots=True)
class Battery:
    id: str
    topology_asset_id: str
    capacity_kwh: Decimal
    max_charge_kw: Decimal
    max_discharge_kw: Decimal
    charge_efficiency: Decimal
    discharge_efficiency: Decimal
    min_soc: Decimal
    max_soc: Decimal
    reserve_soc: Decimal = Decimal(0)
    degradation_cost_per_kwh: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if not 0 < self.charge_efficiency <= 1 or not 0 < self.discharge_efficiency <= 1:
            raise ValueError("battery efficiency must be in (0, 1]")
        if not 0 <= self.min_soc <= self.reserve_soc <= self.max_soc <= 1:
            raise ValueError("invalid battery SOC bounds")


@dataclass(frozen=True, slots=True)
class Generator:
    id: str
    topology_asset_id: str
    max_power_kw: Decimal
    start_delay_minutes: int
    minimum_run_minutes: int
    fuel_kwh: Decimal
    fuel_per_kwh: Decimal
