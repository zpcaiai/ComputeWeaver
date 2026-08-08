from __future__ import annotations

from datetime import datetime

from .prometheus import MetricSample, PrometheusConnector

DCGM_QUERIES = {
    "gpu_power_w": "DCGM_FI_DEV_POWER_USAGE",
    "gpu_utilization_ratio": "DCGM_FI_DEV_GPU_UTIL / 100",
    "gpu_memory_used_bytes": "DCGM_FI_DEV_FB_USED * 1024 * 1024",
    "gpu_temperature_c": "DCGM_FI_DEV_GPU_TEMP",
    "gpu_ecc_errors": "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL",
}


class DcgmConnector:
    """DCGM exporter reader with fixed, reviewed PromQL rather than caller-provided queries."""

    def __init__(self, base_url: str, *, token: str | None = None, ca_bundle: str | bool = True) -> None:
        self.prometheus = PrometheusConnector(
            base_url=base_url,
            queries=DCGM_QUERIES,
            token=token,
            ca_bundle=ca_bundle,
        )

    def query(
        self,
        signal: str,
        *,
        start: datetime,
        end: datetime,
        step_seconds: int = 15,
    ) -> tuple[MetricSample, ...]:
        return self.prometheus.query_range(signal, start=start, end=end, step_seconds=step_seconds)
