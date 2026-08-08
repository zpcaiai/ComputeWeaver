from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo


def local_billing_date(moment: datetime, timezone: str) -> date:
    if moment.tzinfo is None:
        raise ValueError("billing timestamp must be timezone-aware")
    return moment.astimezone(ZoneInfo(timezone)).date()


def normalize_local(moment: datetime, timezone: str) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(timezone))
    return moment.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class BillingCalendar:
    timezone: str
    holidays: frozenset[date] = frozenset()
    weekend_days: frozenset[int] = frozenset({5, 6})

    def is_business_day(self, day: date) -> bool:
        return day.weekday() not in self.weekend_days and day not in self.holidays

    def next_business_day(self, day: date) -> date:
        candidate = day + timedelta(days=1)
        for _ in range(370):
            if self.is_business_day(candidate):
                return candidate
            candidate += timedelta(days=1)
        raise RuntimeError("billing calendar has no business day in bounded search")
