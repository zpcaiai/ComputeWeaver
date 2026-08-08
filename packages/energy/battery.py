from __future__ import annotations

from decimal import Decimal

from .assets import Battery


def next_soc(
    asset: Battery,
    soc: Decimal,
    *,
    charge_kw: Decimal,
    discharge_kw: Decimal,
    duration_hours: Decimal,
) -> Decimal:
    if charge_kw < 0 or discharge_kw < 0 or duration_hours <= 0:
        raise ValueError("battery dispatch values must be non-negative and duration positive")
    if charge_kw > 0 and discharge_kw > 0:
        raise ValueError("battery cannot charge and discharge simultaneously")
    if charge_kw > asset.max_charge_kw or discharge_kw > asset.max_discharge_kw:
        raise ValueError("battery power limit exceeded")
    stored_delta = charge_kw * asset.charge_efficiency * duration_hours
    withdrawn_delta = discharge_kw / asset.discharge_efficiency * duration_hours
    result = soc + (stored_delta - withdrawn_delta) / asset.capacity_kwh
    if not asset.min_soc <= result <= asset.max_soc:
        raise ValueError("battery SOC bound violated")
    return result


def degradation_cost(asset: Battery, charge_kw: Decimal, discharge_kw: Decimal, duration_hours: Decimal) -> Decimal:
    throughput = (charge_kw + discharge_kw) * duration_hours
    return throughput * asset.degradation_cost_per_kwh
