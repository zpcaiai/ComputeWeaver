from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from apps.worker.main import Worker
from packages.connectors.base import CircuitBreaker, ConnectorState
from packages.jobs.queue import DurableJob
from packages.mpc.controller import MpcController
from packages.mpc.state_estimator import ObservedState
from packages.optimization.engine import OptimizationResult
from packages.persistence.operations import OperationIdempotency
from packages.resilience.planner import CriticalLoad, plan_degraded_mode


def test_connector_circuit_opens_and_half_open_recovery_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = 100.0
    monkeypatch.setattr("packages.connectors.base.time.monotonic", lambda: clock)
    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=10)

    def unavailable() -> None:
        raise TimeoutError("provider timeout")

    with pytest.raises(ConnectionError):
        breaker.execute(unavailable, retries=1)
    assert breaker.state == ConnectorState.OPEN
    with pytest.raises(ConnectionError, match="open"):
        breaker.execute(lambda: "unsafe")
    clock = 111.0
    assert breaker.state == ConnectorState.DEGRADED
    assert breaker.execute(lambda: "recovered") == "recovered"
    assert breaker.state == ConnectorState.HEALTHY


def test_failed_idempotent_operation_can_retry_without_duplicate_success() -> None:
    store = OperationIdempotency()
    attempts = 0

    def operation() -> dict[str, int]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("database failover")
        return {"attempt": attempts}

    with pytest.raises(ConnectionError):
        store.execute_once(
            tenant_id="tenant-chaos",
            key="chaos-operation-0001",
            operation="worker.recovery",
            intent={"job": 1},
            callback=operation,
        )
    recovered = store.execute_once(
        tenant_id="tenant-chaos",
        key="chaos-operation-0001",
        operation="worker.recovery",
        intent={"job": 1},
        callback=operation,
    )
    replay = store.execute_once(
        tenant_id="tenant-chaos",
        key="chaos-operation-0001",
        operation="worker.recovery",
        intent={"job": 1},
        callback=operation,
    )
    assert recovered == replay == {"attempt": 2}
    assert attempts == 2


def test_solver_timeout_uses_only_the_last_validated_safe_plan(
    schedule_input: Any,
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = ObservedState(frozenset(), {}, 2, Decimal("0.5"), Decimal(100), Decimal(1), "state-v1")
    controller = MpcController()
    safe = controller.cycle(schedule_input, observed, started_at=now)
    assert safe.fallback_status is None
    remaining = replace(schedule_input, slots=schedule_input.slots[1:])
    monkeypatch.setattr(
        "packages.mpc.controller.optimize",
        lambda *_args, **_kwargs: OptimizationResult(
            "timeout", None, {}, "none", Decimal(1), 1, (), ("SOLVER_TIMEOUT",)
        ),
    )
    recovered = controller.cycle(remaining, observed, started_at=now + timedelta(hours=1))
    assert recovered.fallback_status == "last_safe_plan"
    assert recovered.new_plan == safe.new_plan
    assert "SOLVER_TIMEOUT" in recovered.revision_reasons


def test_worker_failure_records_retry_instead_of_acknowledging_success() -> None:
    job = DurableJob(
        1,
        "tenant-chaos",
        "failing-job",
        {},
        "worker-chaos-0001",
        1,
        3,
        datetime.now(UTC) + timedelta(minutes=1),
    )

    class Queue:
        failed = False
        succeeded = False

        def claim(self, *, worker_id: str) -> DurableJob | None:
            return job

        def fail(self, claimed: DurableJob, *, worker_id: str, error: str) -> str:
            assert claimed == job and worker_id == "worker-chaos" and "provider unavailable" in error
            self.failed = True
            return "pending"

        def succeed(
            self,
            claimed: DurableJob,
            *,
            worker_id: str,
            result: dict[str, Any],
        ) -> None:
            self.succeeded = True

    async def unavailable(_job: DurableJob) -> dict[str, Any]:
        raise ConnectionError("provider unavailable")

    queue = Queue()
    worker = Worker(queue, worker_id="worker-chaos", handlers={"failing-job": unavailable})  # type: ignore[arg-type]
    assert asyncio.run(worker.run_once()) is True
    assert queue.failed is True
    assert queue.succeeded is False
    assert worker.processed == 0


def test_site_power_loss_sheds_noncritical_load_before_reporting_hard_violation() -> None:
    loads = (
        CriticalLoad("control-plane", Decimal(5), 100, True),
        CriticalLoad("training", Decimal(8), 20, False),
        CriticalLoad("batch", Decimal(4), 10, False),
    )
    degraded = plan_degraded_mode(loads, Decimal(6))
    assert degraded.served == ("control-plane",)
    assert set(degraded.shed) == {"training", "batch"}
    assert degraded.hard_violations == ()
    unsafe = plan_degraded_mode(loads, Decimal(4))
    assert unsafe.hard_violations == ("CRITICAL_LOAD_SHED:control-plane",)
