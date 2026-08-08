from __future__ import annotations

import math
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

import httpx


@dataclass(frozen=True, slots=True)
class LoadThresholds:
    p95_ms: float
    p99_ms: float
    max_error_rate: float


@dataclass(frozen=True, slots=True)
class LoadReport:
    status: str
    target: str
    requests: int
    concurrency: int
    successes: int
    errors: int
    error_rate: float
    requests_per_second: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    thresholds: LoadThresholds
    production_evidence: bool
    release_id: str | None
    source_revision: str | None
    request_sha256: str | None


def validate_load_target(target: str) -> None:
    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("load target must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("load target must not contain credentials, query parameters or fragments")
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("non-local load targets must use HTTPS")


def validate_production_contract(
    *,
    target: str,
    requests: int,
    concurrency: int,
    thresholds: LoadThresholds,
    contract: dict[str, Any],
) -> None:
    approved_target = str(contract.get("target", ""))
    if not approved_target or target != approved_target:
        raise ValueError("load target does not match the approved evidence request")
    if requests < max(1000, int(contract.get("minimum_requests", 1000))):
        raise ValueError("load request count is below the approved minimum")
    if concurrency < max(25, int(contract.get("minimum_concurrency", 25))):
        raise ValueError("load concurrency is below the approved minimum")
    maximum_p95 = min(300, float(contract.get("maximum_p95_ms", 300)))
    maximum_p99 = min(1000, float(contract.get("maximum_p99_ms", 1000)))
    maximum_error_rate = min(0.001, float(contract.get("maximum_error_rate", 0.001)))
    if thresholds.p95_ms > maximum_p95 or thresholds.p99_ms > maximum_p99:
        raise ValueError("load latency thresholds are weaker than the approved contract")
    if thresholds.max_error_rate > maximum_error_rate:
        raise ValueError("load error threshold is weaker than the approved contract")


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def _request(target: str, timeout_seconds: float) -> tuple[bool, float]:
    started = time.perf_counter()
    try:
        response = httpx.get(target, timeout=timeout_seconds, follow_redirects=False)
        success = 200 <= response.status_code < 400
    except httpx.HTTPError:
        success = False
    return success, (time.perf_counter() - started) * 1000


def run_load_gate(
    *,
    target: str,
    requests: int,
    concurrency: int,
    thresholds: LoadThresholds,
    timeout_seconds: float = 10,
    requester: Callable[[str, float], tuple[bool, float]] = _request,
    release_id: str | None = None,
    source_revision: str | None = None,
    request_sha256: str | None = None,
) -> LoadReport:
    validate_load_target(target)
    if requests < 1 or concurrency < 1 or concurrency > requests:
        raise ValueError("load request and concurrency counts are invalid")
    if thresholds.p95_ms <= 0 or thresholds.p99_ms < thresholds.p95_ms:
        raise ValueError("load latency thresholds are invalid")
    if not 0 <= thresholds.max_error_rate < 1:
        raise ValueError("load error threshold is invalid")
    started = time.perf_counter()
    results: list[tuple[bool, float]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(requester, target, timeout_seconds) for _ in range(requests)]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception:
                results.append((False, 0.0))
    elapsed = max(time.perf_counter() - started, 0.000001)
    latencies = [latency for _, latency in results]
    successes = sum(1 for success, _ in results if success)
    errors = requests - successes
    error_rate = errors / requests
    p50 = percentile(latencies, 0.50)
    p95 = percentile(latencies, 0.95)
    p99 = percentile(latencies, 0.99)
    latency_passed = p95 <= thresholds.p95_ms and p99 <= thresholds.p99_ms
    hostname = urlparse(target).hostname
    production = hostname not in {"127.0.0.1", "localhost", "::1"}
    bound = not production or bool(release_id and source_revision)
    passed = error_rate <= thresholds.max_error_rate and latency_passed and bound
    return LoadReport(
        "PASS" if passed else "FAIL",
        target,
        requests,
        concurrency,
        successes,
        errors,
        error_rate,
        requests / elapsed,
        p50,
        p95,
        p99,
        thresholds,
        production and bound,
        release_id,
        source_revision,
        request_sha256,
    )


def report_dict(report: LoadReport) -> dict[str, object]:
    return asdict(report)
