from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from threading import RLock

from packages.ingestion.normalize import NormalizedPoint
from packages.persistence.postgres import PostgresRuntime


class TimeSeriesStore:
    def __init__(self, runtime: PostgresRuntime | None = None) -> None:
        self._runtime = runtime
        self._points: dict[tuple[str, str], dict[datetime, NormalizedPoint]] = {}
        self._lock = RLock()

    def append(self, point: NormalizedPoint) -> bool:
        if self._runtime:
            with self._runtime.tenant_connection(point.tenant_id) as connection:
                row = connection.execute(
                    """
                    INSERT INTO timeseries_points(
                      tenant_id, metric, observed_at, value, unit, source,
                      source_event_id, raw_payload_hash, transformation
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, metric, observed_at) DO NOTHING
                    RETURNING observed_at
                    """,
                    (
                        point.tenant_id,
                        point.metric,
                        point.timestamp,
                        point.value,
                        point.unit,
                        point.source,
                        point.raw_event_id,
                        point.raw_payload_hash,
                        point.transformation,
                    ),
                ).fetchone()
                if row:
                    return True
                persisted = connection.execute(
                    """
                    SELECT value, unit FROM timeseries_points
                    WHERE tenant_id = %s AND metric = %s AND observed_at = %s
                    """,
                    (point.tenant_id, point.metric, point.timestamp),
                ).fetchone()
                if persisted and Decimal(persisted["value"]) == point.value and persisted["unit"] == point.unit:
                    return False
                raise ValueError("conflicting point at identical timestamp")
        key = (point.tenant_id, point.metric)
        with self._lock:
            series = self._points.setdefault(key, {})
            memory_point = series.get(point.timestamp)
            if memory_point:
                if memory_point.value == point.value and memory_point.unit == point.unit:
                    return False
                raise ValueError("conflicting point at identical timestamp")
            series[point.timestamp] = point
            return True

    def query(self, tenant_id: str, metric: str, start: datetime, end: datetime) -> tuple[NormalizedPoint, ...]:
        if self._runtime:
            with self._runtime.tenant_connection(tenant_id) as connection:
                rows = connection.execute(
                    """
                    SELECT observed_at, value, unit, source, source_event_id,
                           raw_payload_hash, transformation
                    FROM timeseries_points
                    WHERE tenant_id = %s AND metric = %s
                      AND observed_at >= %s AND observed_at < %s
                    ORDER BY observed_at
                    """,
                    (tenant_id, metric, start, end),
                ).fetchall()
                return tuple(
                    NormalizedPoint(
                        id=f"norm-{row['source_event_id']}",
                        tenant_id=tenant_id,
                        source=str(row["source"]),
                        metric=metric,
                        timestamp=row["observed_at"],
                        value=Decimal(row["value"]),
                        unit=str(row["unit"]),
                        raw_event_id=str(row["source_event_id"]),
                        raw_payload_hash=str(row["raw_payload_hash"]),
                        transformation=str(row["transformation"]),
                    )
                    for row in rows
                )
        points = self._points.get((tenant_id, metric), {})
        return tuple(points[moment] for moment in sorted(points) if start <= moment < end)

    def resample_mean(
        self,
        tenant_id: str,
        metric: str,
        start: datetime,
        end: datetime,
        step: timedelta,
    ) -> tuple[tuple[datetime, Decimal | None], ...]:
        if step.total_seconds() <= 0:
            raise ValueError("resample step must be positive")
        points = self.query(tenant_id, metric, start, end)
        result: list[tuple[datetime, Decimal | None]] = []
        cursor = start
        while cursor < end:
            following = min(end, cursor + step)
            values = [point.value for point in points if cursor <= point.timestamp < following]
            result.append((cursor, sum(values, Decimal(0)) / len(values) if values else None))
            cursor = following
        return tuple(result)
