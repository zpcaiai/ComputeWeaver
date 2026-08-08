from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from packages.ingestion.normalize import NormalizedPoint


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    score: Decimal
    status: str
    missing: int
    stale: int
    duplicates: int
    outliers: int
    conflicts: int
    reasons: tuple[str, ...]


def assess(
    points: tuple[NormalizedPoint, ...],
    *,
    expected_start: datetime,
    expected_end: datetime,
    expected_step: timedelta,
    now: datetime,
    stale_after: timedelta,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> QualityAssessment:
    expected = max(1, int((expected_end - expected_start) / expected_step))
    unique_times = {point.timestamp for point in points}
    duplicates = len(points) - len(unique_times)
    missing = max(0, expected - len(unique_times))
    stale = sum(1 for point in points if now - point.timestamp > stale_after)
    outliers = sum(
        1
        for point in points
        if (minimum is not None and point.value < minimum) or (maximum is not None and point.value > maximum)
    )
    conflict_keys: dict[datetime, set[Decimal]] = {}
    for point in points:
        conflict_keys.setdefault(point.timestamp, set()).add(point.value)
    conflicts = sum(1 for values in conflict_keys.values() if len(values) > 1)
    penalty = Decimal(missing + stale + duplicates + outliers + conflicts) / Decimal(max(expected, len(points), 1))
    score = max(Decimal(0), Decimal(1) - penalty)
    reasons = tuple(
        name
        for name, count in (
            ("missing", missing),
            ("stale", stale),
            ("duplicate", duplicates),
            ("outlier", outliers),
            ("conflict", conflicts),
        )
        if count
    )
    status = "good" if score >= Decimal("0.9") else ("degraded" if score >= Decimal("0.6") else "bad")
    return QualityAssessment(score, status, missing, stale, duplicates, outliers, conflicts, reasons)
