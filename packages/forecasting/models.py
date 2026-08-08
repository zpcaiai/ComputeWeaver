from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ObservedValue:
    timestamp: datetime
    value: Decimal


@dataclass(frozen=True, slots=True)
class ForecastPoint:
    timestamp: datetime
    point: Decimal
    lower: Decimal
    upper: Decimal
    quantiles: dict[str, Decimal]

    def __post_init__(self) -> None:
        if not self.lower <= self.point <= self.upper:
            raise ValueError("forecast interval must contain point estimate")


@dataclass(frozen=True, slots=True)
class ForecastBundle:
    id: str
    signal: str
    points: tuple[ForecastPoint, ...]
    model_version: str
    generated_at: datetime
    valid_for: tuple[datetime, datetime]
    quality: Decimal
    lineage: tuple[str, ...]
    fallback: str | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.quality <= 1:
            raise ValueError("forecast quality must be in [0,1]")
        timestamps = [item.timestamp for item in self.points]
        if timestamps != sorted(set(timestamps)):
            raise ValueError("forecast points must be unique and ordered")


class PersistenceModel:
    name = "persistence"

    def forecast(
        self,
        history: tuple[ObservedValue, ...],
        *,
        start: datetime,
        periods: int,
        step: timedelta,
        model_version: str = "persistence-1",
        signal: str = "unknown",
    ) -> ForecastBundle:
        if not history:
            raise ValueError("history is required")
        if history[-1].timestamp >= start:
            raise ValueError("future-data leakage: training observation overlaps forecast window")
        values = [item.value for item in history]
        point = values[-1]
        ordered = sorted(values)
        lower = ordered[max(0, int(len(ordered) * 0.1) - 1)]
        upper = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
        lower, upper = min(lower, point), max(upper, point)
        points = tuple(
            ForecastPoint(
                timestamp=start + index * step,
                point=point,
                lower=lower,
                upper=upper,
                quantiles={"p10": lower, "p50": point, "p90": upper},
            )
            for index in range(periods)
        )
        return ForecastBundle(
            id=f"forecast-{signal}-{int(start.timestamp())}",
            signal=signal,
            points=points,
            model_version=model_version,
            generated_at=start,
            valid_for=(start, start + periods * step),
            quality=Decimal("0.7"),
            lineage=tuple(item.timestamp.isoformat() for item in history),
        )


class SeasonalModel(PersistenceModel):
    name = "seasonal"

    def forecast(
        self,
        history: tuple[ObservedValue, ...],
        *,
        start: datetime,
        periods: int,
        step: timedelta,
        model_version: str = "seasonal-1",
        signal: str = "unknown",
    ) -> ForecastBundle:
        if not history or history[-1].timestamp >= start:
            raise ValueError("leakage-safe historical data is required")
        seasonal: dict[tuple[int, int], list[Decimal]] = {}
        for item in history:
            seasonal.setdefault((item.timestamp.hour, item.timestamp.minute), []).append(item.value)
        points: list[ForecastPoint] = []
        fallback = history[-1].value
        for index in range(periods):
            timestamp = start + index * step
            values = seasonal.get((timestamp.hour, timestamp.minute), [fallback])
            point = sum(values, Decimal(0)) / len(values)
            spread = max(values) - min(values)
            points.append(
                ForecastPoint(
                    timestamp,
                    point,
                    point - spread,
                    point + spread,
                    {"p10": point - spread, "p50": point, "p90": point + spread},
                )
            )
        return ForecastBundle(
            id=f"forecast-{signal}-{int(start.timestamp())}",
            signal=signal,
            points=tuple(points),
            model_version=model_version,
            generated_at=start,
            valid_for=(start, start + periods * step),
            quality=Decimal("0.8"),
            lineage=tuple(item.timestamp.isoformat() for item in history),
        )
