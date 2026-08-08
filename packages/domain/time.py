from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .units import Duration


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class TimeInterval:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        ensure_aware(self.start)
        ensure_aware(self.end)
        if self.end <= self.start:
            raise ValueError("interval must be half-open with end > start")

    @property
    def duration(self) -> Duration:
        return Duration((self.end - self.start).total_seconds() / 3600)

    def contains(self, moment: datetime) -> bool:
        ensure_aware(moment)
        return self.start <= moment < self.end

    def overlaps(self, other: TimeInterval) -> bool:
        return self.start < other.end and other.start < self.end

    def split(self, step: timedelta) -> tuple[TimeInterval, ...]:
        if step.total_seconds() <= 0:
            raise ValueError("step must be positive")
        result: list[TimeInterval] = []
        current = self.start
        while current < self.end:
            following = min(current + step, self.end)
            result.append(TimeInterval(current, following))
            current = following
        return tuple(result)

    def in_timezone(self, timezone: str) -> tuple[datetime, datetime]:
        zone = ZoneInfo(timezone)
        return self.start.astimezone(zone), self.end.astimezone(zone)
