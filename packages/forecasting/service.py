from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from .models import ForecastBundle, ObservedValue, PersistenceModel, SeasonalModel


class ForecastService:
    def __init__(self, minimum_quality: Decimal = Decimal("0.6")) -> None:
        if not 0 <= minimum_quality <= 1:
            raise ValueError("minimum forecast quality must be in [0,1]")
        self.minimum_quality = minimum_quality

    def generate(
        self,
        history: tuple[ObservedValue, ...],
        *,
        start: datetime,
        periods: int,
        step: timedelta,
        signal: str,
        preferred_model: str = "seasonal",
    ) -> ForecastBundle:
        if periods < 1 or step <= timedelta(0):
            raise ValueError("forecast horizon must be positive")
        models = {"persistence": PersistenceModel(), "seasonal": SeasonalModel()}
        try:
            model = models[preferred_model]
        except KeyError as error:
            raise ValueError(f"unsupported forecast model {preferred_model}") from error
        bundle = model.forecast(history, start=start, periods=periods, step=step, signal=signal)
        if bundle.quality >= self.minimum_quality:
            return bundle
        fallback = PersistenceModel().forecast(
            history,
            start=start,
            periods=periods,
            step=step,
            model_version="persistence-fallback-1",
            signal=signal,
        )
        return replace(fallback, fallback=f"quality_below_{self.minimum_quality}")
