from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .models import ObservedValue


@dataclass(frozen=True, slots=True)
class FeatureRow:
    timestamp: datetime
    target: Decimal
    lags: tuple[Decimal, ...]
    hour: int
    weekday: int


def build_lag_features(
    history: tuple[ObservedValue, ...],
    *,
    cutoff: datetime,
    lag_steps: tuple[int, ...] = (1, 24),
    expected_step: timedelta | None = None,
) -> tuple[FeatureRow, ...]:
    if cutoff.tzinfo is None or any(item.timestamp.tzinfo is None for item in history):
        raise ValueError("feature timestamps must be timezone-aware")
    if any(step < 1 for step in lag_steps):
        raise ValueError("lag steps must be positive")
    ordered = tuple(sorted(history, key=lambda item: item.timestamp))
    if len({item.timestamp for item in ordered}) != len(ordered):
        raise ValueError("duplicate feature timestamps")
    if any(item.timestamp >= cutoff for item in ordered):
        raise ValueError("future-data leakage detected")
    if expected_step is not None and any(
        right.timestamp - left.timestamp != expected_step for left, right in zip(ordered, ordered[1:], strict=False)
    ):
        raise ValueError("feature history has an unexpected gap")
    maximum = max(lag_steps, default=0)
    return tuple(
        FeatureRow(
            ordered[index].timestamp,
            ordered[index].value,
            tuple(ordered[index - lag].value for lag in lag_steps),
            ordered[index].timestamp.hour,
            ordered[index].timestamp.weekday(),
        )
        for index in range(maximum, len(ordered))
    )
