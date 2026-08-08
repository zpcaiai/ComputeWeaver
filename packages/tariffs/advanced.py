from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from packages.domain.units import Money


@dataclass(frozen=True, slots=True)
class EnergyTier:
    up_to_kwh: Decimal | None
    price_per_kwh: Decimal

    def __post_init__(self) -> None:
        if self.up_to_kwh is not None and self.up_to_kwh <= 0:
            raise ValueError("tier boundary must be positive")
        if self.price_per_kwh < 0:
            raise ValueError("tier price cannot be negative")


def calculate_tiered_energy(import_kwh: Decimal, tiers: tuple[EnergyTier, ...], currency: str) -> Money:
    if import_kwh < 0 or not tiers or tiers[-1].up_to_kwh is not None:
        raise ValueError("tiered tariff requires nonnegative usage and an open final tier")
    finite = [tier.up_to_kwh for tier in tiers if tier.up_to_kwh is not None]
    if finite != sorted(set(finite)):
        raise ValueError("tier boundaries must be unique and increasing")
    remaining = import_kwh
    lower = Decimal(0)
    cost = Decimal(0)
    for tier in tiers:
        width = remaining if tier.up_to_kwh is None else min(remaining, tier.up_to_kwh - lower)
        width = max(Decimal(0), width)
        cost += width * tier.price_per_kwh
        remaining -= width
        if remaining <= 0:
            break
        if tier.up_to_kwh is not None:
            lower = tier.up_to_kwh
    return Money(cost, currency).rounded()


@dataclass(frozen=True, slots=True)
class RealTimePrice:
    observed_at: datetime
    price_per_kwh: Decimal


def real_time_price_at(
    prices: tuple[RealTimePrice, ...],
    moment: datetime,
    *,
    maximum_age: timedelta,
) -> Decimal:
    if moment.tzinfo is None or maximum_age <= timedelta(0):
        raise ValueError("real-time price lookup requires aware time and positive maximum age")
    if any(item.observed_at.tzinfo is None or item.price_per_kwh < 0 for item in prices):
        raise ValueError("real-time prices must be aware and nonnegative")
    candidates = [item for item in prices if item.observed_at <= moment]
    if not candidates:
        raise LookupError("no real-time price is available")
    selected = max(candidates, key=lambda item: item.observed_at)
    if moment - selected.observed_at > maximum_age:
        raise LookupError("real-time price is stale")
    return selected.price_per_kwh


def subsidy_credit(import_kwh: Decimal, subsidy_per_kwh: Decimal, currency: str) -> Money:
    if import_kwh < 0 or subsidy_per_kwh < 0:
        raise ValueError("subsidy inputs cannot be negative")
    return Money(import_kwh * subsidy_per_kwh, currency).rounded()
