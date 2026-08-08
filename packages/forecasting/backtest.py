from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from .models import ObservedValue, PersistenceModel


@dataclass(frozen=True, slots=True)
class BacktestResult:
    mae: Decimal
    interval_coverage: Decimal
    folds: int


def rolling_origin_backtest(observations: tuple[ObservedValue, ...], minimum_train: int = 4) -> BacktestResult:
    if len(observations) <= minimum_train:
        raise ValueError("insufficient observations")
    errors: list[Decimal] = []
    covered = 0
    model = PersistenceModel()
    for index in range(minimum_train, len(observations)):
        train = observations[:index]
        actual = observations[index]
        forecast = model.forecast(train, start=actual.timestamp, periods=1, step=timedelta(hours=1)).points[0]
        errors.append(abs(forecast.point - actual.value))
        covered += int(forecast.lower <= actual.value <= forecast.upper)
    return BacktestResult(
        mae=sum(errors, Decimal(0)) / len(errors),
        interval_coverage=Decimal(covered) / len(errors),
        folds=len(errors),
    )
