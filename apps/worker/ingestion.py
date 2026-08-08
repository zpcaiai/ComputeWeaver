from __future__ import annotations

from dataclasses import dataclass

from packages.connectors.base import CircuitBreaker
from packages.ingestion.normalize import Normalizer
from packages.ingestion.raw import RawEvent, RawLanding
from packages.timeseries.store import TimeSeriesStore


@dataclass(frozen=True, slots=True)
class IngestionResult:
    accepted_raw: int
    accepted_points: int
    duplicates: int


class IngestionProcessor:
    def __init__(self, raw: RawLanding, points: TimeSeriesStore, breaker: CircuitBreaker | None = None) -> None:
        self.raw = raw
        self.points = points
        self.breaker = breaker or CircuitBreaker()
        self.normalizer = Normalizer()

    def ingest(self, events: tuple[RawEvent, ...]) -> IngestionResult:
        accepted_raw = 0
        accepted_points = 0
        duplicates = 0

        def process() -> None:
            nonlocal accepted_raw, accepted_points, duplicates
            for event in events:
                if self.raw.append(event):
                    accepted_raw += 1
                else:
                    duplicates += 1
                if self.points.append(self.normalizer.normalize(event)):
                    accepted_points += 1

        self.breaker.execute(process, retries=0)
        return IngestionResult(accepted_raw, accepted_points, duplicates)
