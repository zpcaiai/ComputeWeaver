from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .assets import GridConnection
from .pue import facility_power_kw


@dataclass(frozen=True, slots=True)
class Dispatch:
    grid_import_kw: Decimal
    grid_export_kw: Decimal
    pv_kw: Decimal
    battery_charge_kw: Decimal
    battery_discharge_kw: Decimal
    generator_kw: Decimal = Decimal(0)
    curtailed_kw: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class PowerBalanceResult:
    facility_load_kw: Decimal
    supply_kw: Decimal
    demand_kw: Decimal
    residual_kw: Decimal
    violations: tuple[str, ...]


def validate_power_balance(
    *,
    compute_load_kw: Decimal,
    pue: Decimal,
    fixed_load_kw: Decimal,
    dispatch: Dispatch,
    grid: GridConnection,
    tolerance_kw: Decimal = Decimal("0.001"),
) -> PowerBalanceResult:
    values = (
        dispatch.grid_import_kw,
        dispatch.grid_export_kw,
        dispatch.pv_kw,
        dispatch.battery_charge_kw,
        dispatch.battery_discharge_kw,
        dispatch.generator_kw,
        dispatch.curtailed_kw,
    )
    if any(value < 0 for value in values):
        raise ValueError("dispatch powers cannot be negative")
    if dispatch.battery_charge_kw > 0 and dispatch.battery_discharge_kw > 0:
        raise ValueError("simultaneous battery charge/discharge")
    facility = facility_power_kw(compute_load_kw, pue, fixed_load_kw)
    supply = dispatch.grid_import_kw + dispatch.pv_kw + dispatch.battery_discharge_kw + dispatch.generator_kw
    demand = facility + dispatch.battery_charge_kw + dispatch.grid_export_kw + dispatch.curtailed_kw
    residual = supply - demand
    violations: list[str] = []
    if abs(residual) > tolerance_kw:
        violations.append("POWER_BALANCE")
    if dispatch.grid_import_kw > grid.import_limit_kw:
        violations.append("GRID_IMPORT_CAPACITY")
    if dispatch.grid_export_kw > grid.export_limit_kw:
        violations.append("GRID_EXPORT_CAPACITY")
    return PowerBalanceResult(facility, supply, demand, residual, tuple(violations))
