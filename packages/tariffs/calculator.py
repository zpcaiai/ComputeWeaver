from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from packages.domain.units import Money

from .models import TariffPlan


@dataclass(frozen=True, slots=True)
class MeterInterval:
    started_at: datetime
    duration_hours: Decimal
    import_kwh: Decimal
    export_kwh: Decimal = Decimal(0)
    peak_kw: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    energy: Money
    demand: Money
    capacity: Money
    export_credit: Money
    tax: Money
    total: Money
    line_items: tuple[dict[str, str], ...]


class TariffCalculator:
    def calculate(
        self,
        tariff: TariffPlan,
        intervals: tuple[MeterInterval, ...],
        *,
        contracted_capacity_kw: Decimal = Decimal(0),
        demand_response_credit: Decimal = Decimal(0),
        demand_response_penalty: Decimal = Decimal(0),
    ) -> CostBreakdown:
        zone = ZoneInfo(tariff.timezone)
        energy = Decimal(0)
        export_credit = Decimal(0)
        peak = Decimal(0)
        lines: list[dict[str, str]] = []
        for interval in intervals:
            if interval.started_at.tzinfo is None:
                raise ValueError("meter interval timestamp must be timezone-aware")
            local = interval.started_at.astimezone(zone)
            if not tariff.active_on(local.date()):
                raise ValueError("tariff is not active for interval")
            matches = [period for period in tariff.periods if period.matches(local)]
            if len(matches) != 1:
                raise ValueError("tariff periods must cover each interval exactly once")
            period = matches[0]
            line_cost = interval.import_kwh * period.price_per_kwh
            energy += line_cost
            export_credit += interval.export_kwh * tariff.feed_in_price_per_kwh
            peak = max(peak, interval.peak_kw)
            lines.append(
                {
                    "period": period.name,
                    "import_kwh": str(interval.import_kwh),
                    "unit_price": str(period.price_per_kwh),
                    "cost": str(line_cost),
                }
            )
        demand = peak * tariff.demand_charge_per_kw
        capacity = contracted_capacity_kw * tariff.capacity_charge_per_kw
        subtotal = energy + demand + capacity - export_credit - demand_response_credit + demand_response_penalty
        tax = subtotal * tariff.tax_rate
        total = Money(subtotal + tax, tariff.currency).rounded()
        return CostBreakdown(
            energy=Money(energy, tariff.currency).rounded(),
            demand=Money(demand, tariff.currency).rounded(),
            capacity=Money(capacity, tariff.currency).rounded(),
            export_credit=Money(export_credit, tariff.currency).rounded(),
            tax=Money(tax, tariff.currency).rounded(),
            total=total,
            line_items=tuple(lines),
        )


def validate_tariff_versions(plans: tuple[TariffPlan, ...]) -> None:
    by_id: dict[str, list[TariffPlan]] = {}
    for plan in plans:
        by_id.setdefault(plan.id, []).append(plan)
    for versions in by_id.values():
        ordered = sorted(versions, key=lambda item: item.effective_from)
        for left, right in zip(ordered, ordered[1:], strict=False):
            if left.effective_to is None or left.effective_to > right.effective_from:
                raise ValueError("overlapping tariff versions")
