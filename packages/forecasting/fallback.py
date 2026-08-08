from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from .models import ForecastBundle


def require_quality(bundle: ForecastBundle, minimum: Decimal) -> ForecastBundle:
    if bundle.quality >= minimum:
        return bundle
    conservative = tuple(
        replace(point, point=point.upper, lower=point.upper, upper=point.upper) for point in bundle.points
    )
    return replace(bundle, points=conservative, fallback="conservative_upper_bound")
