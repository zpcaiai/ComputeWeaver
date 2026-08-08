from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from packages.domain.units import Carbon, Money


@dataclass(frozen=True, slots=True)
class CarbonResult:
    emissions: Carbon
    carbon_cost: Money


def calculate_carbon(
    import_kwh: Decimal,
    intensity_kg_per_kwh: Decimal,
    price_per_kg: Decimal,
    currency: str = "USD",
) -> CarbonResult:
    if min(import_kwh, intensity_kg_per_kwh, price_per_kg) < 0:
        raise ValueError("carbon inputs cannot be negative")
    emissions = Carbon(import_kwh * intensity_kg_per_kwh)
    return CarbonResult(emissions, Money(emissions.kg_co2e * price_per_kg, currency).rounded())
