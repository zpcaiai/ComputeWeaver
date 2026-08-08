from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class PricePeriod:
    name: str
    start_local: time
    end_local: time
    price_per_kwh: Decimal
    weekdays: frozenset[int] = frozenset(range(7))

    def matches(self, local: datetime) -> bool:
        clock = local.timetz().replace(tzinfo=None)
        in_time = (
            self.start_local <= clock < self.end_local
            if self.start_local < self.end_local
            else clock >= self.start_local or clock < self.end_local
        )
        return local.weekday() in self.weekdays and in_time


@dataclass(frozen=True, slots=True)
class TariffPlan:
    id: str
    version: int
    currency: str
    timezone: str
    effective_from: date
    effective_to: date | None
    periods: tuple[PricePeriod, ...]
    demand_charge_per_kw: Decimal = Decimal(0)
    capacity_charge_per_kw: Decimal = Decimal(0)
    tax_rate: Decimal = Decimal(0)
    feed_in_price_per_kwh: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if not self.periods:
            raise ValueError("tariff requires at least one price period")
        if any(value < 0 for value in (self.demand_charge_per_kw, self.tax_rate)):
            raise ValueError("tariff charges cannot be negative")

    def active_on(self, local_date: date) -> bool:
        return self.effective_from <= local_date and (self.effective_to is None or local_date < self.effective_to)
