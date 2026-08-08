from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import httpx


@dataclass(frozen=True, slots=True)
class MetricSample:
    metric: str
    labels: dict[str, str]
    timestamp: datetime
    value: Decimal


class PrometheusConnector:
    """Bounded Prometheus/DCGM reader using operator-defined query templates."""

    def __init__(
        self,
        *,
        base_url: str,
        queries: dict[str, str],
        token: str | None = None,
        ca_bundle: str | bool = True,
        timeout_seconds: float = 10,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("Prometheus URL must use HTTPS")
        if not queries or any(not name or not query for name, query in queries.items()):
            raise ValueError("Prometheus query map cannot be empty")
        self.base_url = base_url.rstrip("/")
        self.queries = dict(queries)
        self.token = token
        self.ca_bundle = ca_bundle
        self.timeout_seconds = timeout_seconds

    def query_range(
        self,
        signal: str,
        *,
        start: datetime,
        end: datetime,
        step_seconds: int,
    ) -> tuple[MetricSample, ...]:
        if signal not in self.queries:
            raise PermissionError("signal is not in the operator-defined Prometheus allowlist")
        if end <= start or step_seconds < 1:
            raise ValueError("invalid Prometheus query interval")
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        with httpx.Client(
            base_url=self.base_url,
            headers=headers,
            verify=self.ca_bundle,
            timeout=self.timeout_seconds,
            follow_redirects=False,
        ) as client:
            response = client.get(
                "/api/v1/query_range",
                params={
                    "query": self.queries[signal],
                    "start": start.timestamp(),
                    "end": end.timestamp(),
                    "step": step_seconds,
                },
            )
            response.raise_for_status()
            document = response.json()
        if document.get("status") != "success":
            raise ConnectionError("Prometheus query was not successful")
        data = document.get("data", {})
        results = data.get("result") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise ConnectionError("Prometheus response is malformed")
        samples: list[MetricSample] = []
        for series in results:
            if not isinstance(series, dict):
                continue
            labels = series.get("metric", {})
            values = series.get("values", [])
            if not isinstance(labels, dict) or not isinstance(values, list):
                continue
            for raw in values:
                if not isinstance(raw, list) or len(raw) != 2:
                    continue
                samples.append(
                    MetricSample(
                        signal,
                        {str(key): str(value) for key, value in labels.items()},
                        datetime.fromtimestamp(float(raw[0]), tz=start.tzinfo),
                        Decimal(str(raw[1])),
                    )
                )
        return tuple(samples)
