from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from packages.benchmark.runner import benchmark
from packages.forecasting.backtest import rolling_origin_backtest
from packages.forecasting.fallback import require_quality
from packages.forecasting.models import ObservedValue, PersistenceModel
from packages.mpc.controller import MpcController
from packages.mpc.state_estimator import ObservedState
from packages.optimization.engine import optimize
from packages.optimization.solvers import HighsSolver
from packages.scheduling.strategies import (
    schedule_fifo,
    schedule_price_aware,
    schedule_priority_edf,
)


def test_forecast_is_deterministic_leakage_safe_and_falls_back(now) -> None:
    history = tuple(ObservedValue(now - timedelta(hours=6 - index), Decimal(index + 1)) for index in range(6))
    bundle = PersistenceModel().forecast(history, start=now, periods=4, step=timedelta(hours=1), signal="pv")
    assert bundle == PersistenceModel().forecast(history, start=now, periods=4, step=timedelta(hours=1), signal="pv")
    with pytest.raises(ValueError, match="leakage"):
        PersistenceModel().forecast(history, start=history[-1].timestamp, periods=1, step=timedelta(hours=1))
    conservative = require_quality(bundle, Decimal("0.9"))
    assert conservative.fallback == "conservative_upper_bound"
    assert all(point.point == point.upper for point in conservative.points)
    assert rolling_origin_backtest(history, minimum_train=3).folds == 3


def test_three_baselines_are_deterministic_and_benchmark_identical_input(schedule_input) -> None:
    fifo = schedule_fifo(schedule_input)
    priority = schedule_priority_edf(schedule_input)
    price = schedule_price_aware(schedule_input)
    assert fifo == schedule_fifo(schedule_input)
    assert fifo.allocations[0].job_id == "job-one"
    assert priority.allocations[0].job_id == "job-two"
    assert price.allocations[0].slot_indices == (1,)
    result = benchmark(schedule_input, seed=44)
    assert {plan.input_hash for plan in result.plans} == {result.input_hash}


def test_exact_optimizer_known_optimum_and_infeasibility(schedule_input) -> None:
    result = optimize(schedule_input)
    assert result.status == "optimal"
    assert result.gap == 0
    assert result.plan is not None
    assert result.plan.estimated_cost == Decimal("0.36")
    impossible_slots = tuple(replace(slot, gpu_capacity=1) for slot in schedule_input.slots)
    impossible = optimize(replace(schedule_input, slots=impossible_slots))
    assert impossible.status == "infeasible"
    assert any("JOB_NO_FEASIBLE_WINDOW" in item for item in impossible.diagnostics)


def test_highs_milp_matches_exact_solver_and_exports_model(schedule_input, tmp_path) -> None:
    model_path = tmp_path / "model.lp"
    highs = HighsSolver().solve(schedule_input, model_path=model_path)
    exact = optimize(schedule_input)
    assert highs.status == "optimal"
    assert highs.gap == 0
    assert highs.plan is not None and exact.plan is not None
    assert highs.plan.estimated_cost == exact.plan.estimated_cost
    assert model_path.read_text(encoding="utf-8").startswith("\\ File written by HiGHS")


def test_mpc_releases_one_interval_and_blocks_rewrite(schedule_input, now) -> None:
    controller = MpcController()
    observed = ObservedState(frozenset(), {}, 2, Decimal("0.5"), Decimal(100), Decimal(1), "state-v1")
    cycle = controller.cycle(schedule_input, observed, started_at=now)
    assert all(len(item.slot_indices) == 1 for item in cycle.current_allocations)
    with pytest.raises(ValueError, match="executed"):
        controller.cycle(schedule_input, observed, started_at=now + timedelta(hours=1))


def test_mpc_quality_failure_requires_last_safe_plan(schedule_input, now) -> None:
    controller = MpcController()
    poor = ObservedState(frozenset(), {}, 2, Decimal("0.5"), Decimal(100), Decimal("0.2"), "state-v1")
    with pytest.raises(RuntimeError, match="no tested fallback"):
        controller.cycle(schedule_input, poor, started_at=now)
