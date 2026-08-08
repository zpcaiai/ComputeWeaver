from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from apps.api.store import ResourceStore
from apps.simulator.clock import VirtualClock
from apps.simulator.cluster import SimulatedCluster
from apps.simulator.energy import SimulatedEnergySystem
from apps.simulator.snapshots import SnapshotEnvelope, create_snapshot, verify_snapshot
from apps.simulator.workloads import WorkloadArrivalModel
from packages.benchmark.report import compare_strategies
from packages.compute.health import HealthState, SourceObservation, reconcile_health
from packages.execution.compute import ComputeActionAdapter
from packages.execution.energy import EnergyPlanAdapter
from packages.explain.constraints import binding_constraints, explain_constraint
from packages.forecasting.features import build_lag_features
from packages.forecasting.models import ObservedValue
from packages.forecasting.service import ForecastService
from packages.mpc.fallback import select_fallback
from packages.mpc.horizon import partition_horizon
from packages.mpc.repository import MpcRepository
from packages.mpc.stability import score_stability
from packages.mpc.state_estimator import ObservedState
from packages.mpc.warm_start import reusable_allocations
from packages.optimization.constraints import validate_hard_constraints
from packages.optimization.model import ObjectiveWeights, build_dimensions
from packages.optimization.objectives import evaluate_objectives
from packages.policy.dsl import parse_policy_document
from packages.policy.models import Enforcement
from packages.scenarios.compiler import compile_scenario
from packages.scheduling.contracts import ScheduleInput
from packages.scheduling.fifo import schedule as fifo
from packages.scheduling.price_aware import schedule as price_aware
from packages.scheduling.priority_edf import schedule as priority_edf
from packages.simulation.engine import SimulationConfig
from packages.simulation.session import SimulationRepository
from packages.topology.models import Asset, AssetType, Relationship, TopologySnapshot
from packages.topology.versioning import compare_versions
from packages.workloads.sla import SlaObservation, evaluate_sla


def test_topology_version_diff_is_tenant_safe(now: datetime) -> None:
    site = Asset("site", "tenant-one", "site", AssetType.SITE, "Site", Decimal("100"))
    rack = Asset("rack", "tenant-one", "site", AssetType.RACK, "Rack", Decimal("20"))
    left = TopologySnapshot(1, "tenant-one", (site,), (), now, "etag-one")
    right = TopologySnapshot(
        2,
        "tenant-one",
        (replace(site, capacity_kw=Decimal("120")), rack),
        (Relationship("site", "rack"),),
        now + timedelta(minutes=1),
        "etag-two",
    )
    difference = compare_versions(left, right)
    assert difference.added_assets == frozenset({"rack"})
    assert difference.changed_assets[0].fields == ("capacity_kw",)
    assert difference.capacity_delta_kw == Decimal("40")
    with pytest.raises(PermissionError):
        compare_versions(left, replace(right, tenant_id="tenant-two"))


def test_compute_health_reconciles_priority_staleness_and_conflict(now: datetime) -> None:
    result = reconcile_health(
        (
            SourceObservation("scheduler", now - timedelta(seconds=5), {"online": True, "gpu_count": 8}, 10),
            SourceObservation("telemetry", now - timedelta(seconds=4), {"online": False, "gpu_count": 8}, 5),
            SourceObservation("cmdb", now - timedelta(hours=1), {"online": True}, 1),
        ),
        now=now,
        freshness=timedelta(minutes=5),
    )
    assert result.state == HealthState.CONFLICT
    assert result.selected_source == "scheduler"
    assert result.stale_sources == ("cmdb",)
    assert result.conflicts == ("online",)
    stale = reconcile_health((), now=now, freshness=timedelta(minutes=1))
    assert stale.state == HealthState.STALE


def test_workload_sla_detects_risk_deadline_and_latency(sample_jobs: tuple[Any, ...], now: datetime) -> None:
    sla = sample_jobs[0].sla
    healthy = evaluate_sla(sla, SlaObservation(now, completed_at=now + timedelta(hours=1), latency_ms=10))
    assert healthy.met and not healthy.at_risk
    failed = evaluate_sla(
        replace(sla, max_latency_ms=50),
        SlaObservation(
            sla.deadline - timedelta(minutes=1),
            completed_at=sla.deadline + timedelta(seconds=1),
            latency_ms=51,
        ),
    )
    assert failed.violations == ("DEADLINE_MISSED", "LATENCY_EXCEEDED")


def test_forecast_feature_pipeline_blocks_leakage_and_service_falls_back(now: datetime) -> None:
    history = tuple(ObservedValue(now + timedelta(hours=index), Decimal(index)) for index in range(30))
    cutoff = now + timedelta(hours=31)
    rows = build_lag_features(history, cutoff=cutoff, expected_step=timedelta(hours=1))
    assert rows[0].lags == (Decimal(23), Decimal(0))
    forecast = ForecastService().generate(
        history,
        start=cutoff,
        periods=3,
        step=timedelta(hours=1),
        signal="facility_power",
    )
    assert len(forecast.points) == 3
    fallback = ForecastService(Decimal("0.9")).generate(
        history,
        start=cutoff,
        periods=1,
        step=timedelta(hours=1),
        signal="facility_power",
    )
    assert fallback.fallback == "quality_below_0.9"
    with pytest.raises(ValueError, match="leakage"):
        build_lag_features(history, cutoff=history[-1].timestamp)


def test_baseline_modules_report_and_optimizer_catalog(schedule_input: ScheduleInput) -> None:
    plans = fifo(schedule_input), priority_edf(schedule_input), price_aware(schedule_input)
    assert {plan.strategy for plan in plans} == {"fifo", "priority_edf", "price_aware"}
    report = compare_strategies(plans[0], plans[2])
    assert report.baseline == "fifo"
    dimensions = build_dimensions(schedule_input)
    assert dimensions.jobs == ("job-one", "job-two")
    assert validate_hard_constraints(schedule_input) == ()
    objectives = evaluate_objectives(
        schedule_input,
        plans[2],
        ObjectiveWeights(energy_cost=Decimal(1), carbon=Decimal(1), delay=Decimal("0.1")),
    )
    assert objectives.weighted_total >= objectives.energy_cost
    with pytest.raises(ValueError, match="at least one"):
        ObjectiveWeights(energy_cost=Decimal(0))


def test_mpc_support_modules_partition_reuse_stability_and_fallback(schedule_input: ScheduleInput) -> None:
    plan = fifo(schedule_input)
    horizon = partition_horizon(schedule_input.slots, locked_count=1, controllable_count=2)
    assert len(horizon.locked) == 1 and len(horizon.forecast_only) == 1
    assert reusable_allocations(plan, schedule_input) == plan.allocations
    stability = score_stability(plan, price_aware(schedule_input), maximum_churn=Decimal(1))
    assert stability.acceptable
    fallback = select_fallback(last_safe_plan=plan, data_quality_ok=True, emergency_shedding_allowed=False)
    assert fallback.automation_allowed and fallback.plan is plan
    hold = select_fallback(last_safe_plan=None, data_quality_ok=False, emergency_shedding_allowed=False)
    assert hold.mode == "hold" and not hold.automation_allowed


def test_mpc_repository_persists_controller_state_and_cycle(schedule_input: ScheduleInput, now: datetime) -> None:
    store = ResourceStore()
    repository = MpcRepository(store)
    controller = repository.create(
        "controller-one",
        "tenant-one",
        idempotency_key="mpc-create-0001",
    )
    assert controller["version"] == 1
    cycle = repository.cycle(
        "controller-one",
        "tenant-one",
        schedule_input,
        ObservedState(frozenset(), {}, 2, Decimal("0.5"), Decimal("100"), Decimal("0.9"), "state-v1"),
        started_at=now,
        timeout_seconds=2,
        idempotency_key="mpc-cycle-0001",
    )
    assert cycle.id == 1
    assert store.get("mpc_controller_state", "controller-one", "tenant-one").body["cycle_sequence"] == 1
    assert store.get("mpc_cycle", "controller-one-1", "tenant-one").body["cycle_id"] == 1


def test_versioned_policy_dsl_rejects_unknown_fields() -> None:
    document = {
        "schema_version": "1.0",
        "id": "grid-policy",
        "version": 1,
        "site_ids": ["site-one"],
        "rule": {"field": "grid_kw", "operator": "lte", "value": 100},
        "enforcement": "hard",
        "priority": 100,
        "owner": "safety",
    }
    policy = parse_policy_document(document, tenant_id="tenant-one")
    assert policy.enforcement == Enforcement.HARD
    with pytest.raises(ValueError, match="unknown"):
        parse_policy_document({**document, "script": "unsafe"}, tenant_id="tenant-one")


class _Gateway:
    def dry_run(self, target: str, kind: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"valid": True, "target": target, "kind": kind, "parameters": parameters}

    def execute(
        self,
        target: str,
        kind: str,
        parameters: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return {"status": "accepted", "key": idempotency_key, "kind": kind, "target": target}


def test_compute_and_energy_adapters_allow_only_high_level_bounded_actions() -> None:
    compute = ComputeActionAdapter(_Gateway())
    assert (
        compute.execute("scheduler", "schedule_job", {"job_id": "job-one"}, idempotency_key="compute-0001")["status"]
        == "accepted"
    )
    with pytest.raises(PermissionError, match="allowlisted"):
        compute.dry_run("scheduler", "shell", {"command": "rm"})

    energy = EnergyPlanAdapter(_Gateway(), Decimal("100"))
    plan = {"dispatch_kw": "20", "valid_until": "2026-01-01T00:05:00Z"}
    assert energy.dry_run("ems", plan)["valid"] is True
    with pytest.raises(PermissionError, match="low-level"):
        energy.validate({**plan, "breaker": "open"})
    with pytest.raises(ValueError, match="bound"):
        energy.validate({"dispatch_kw": "101", "valid_until": "later"})


def test_constraint_explanation_and_strict_scenario_schema() -> None:
    grid = explain_constraint("GRID_CAPACITY", actual=Decimal("99"), limit=Decimal("100"))
    battery = explain_constraint("BATTERY_RESERVE", actual=Decimal("11"), limit=Decimal("10"))
    assert binding_constraints((grid, battery)) == ("GRID_CAPACITY", "BATTERY_RESERVE")
    scenario = compile_scenario(
        {
            "name": "gpu-failure",
            "version": "1.0.0",
            "seed": 7,
            "duration_hours": 1,
            "faults": [{"step": 1, "kind": "gpu_failure", "target": "node-one"}],
        }
    )
    assert scenario.faults[0].kind == "gpu_failure"
    with pytest.raises(ValueError, match="version"):
        compile_scenario({"name": "bad", "version": "2", "seed": 1, "duration_hours": 1})


def test_simulator_components_are_deterministic_and_snapshot_safe(now: datetime) -> None:
    clock = VirtualClock(now, timedelta(minutes=15), speed=4)
    clock.start()
    assert clock.step(2) == now + timedelta(minutes=30)
    clock.pause()
    assert not clock.running

    arrivals = WorkloadArrivalModel(seed=4, arrival_probability=0)
    jobs = arrivals.next(burst=2)
    cluster = SimulatedCluster(4)
    cluster.submit(jobs)
    assert cluster.snapshot().available_gpus == 0
    cluster.step()

    energy = SimulatedEnergySystem(battery_capacity_kwh=Decimal("100"))
    result = energy.step(
        facility_kw=Decimal("80"),
        pv_kw=Decimal("10"),
        grid_limit_kw=Decimal("50"),
        duration_hours=Decimal("0.25"),
    )
    assert result.grid_kw == Decimal("50")
    assert result.unserved_kw == 0

    snapshot = create_snapshot({"clock": clock.now, "jobs": [job.id for job in jobs]})
    assert verify_snapshot(snapshot)
    assert not verify_snapshot(SnapshotEnvelope(snapshot.schema_version, {"tampered": True}, snapshot.sha256))


def test_persistent_simulation_session_restores_identical_future() -> None:
    repository = SimulationRepository(ResourceStore())
    created = repository.create(
        "simulation-one",
        "tenant-one",
        SimulationConfig(duration_hours=1, seed=12),
        idempotency_key="simulation-create-0001",
    )
    assert created["version"] == 1
    repository.operate(
        "simulation-one",
        "tenant-one",
        "step",
        {"fault": "job_burst"},
        idempotency_key="simulation-step-0001",
    )
    snapshot = repository.operate(
        "simulation-one",
        "tenant-one",
        "snapshot",
        {},
        idempotency_key="simulation-snapshot-0001",
    )
    future = repository.operate(
        "simulation-one",
        "tenant-one",
        "step",
        {},
        idempotency_key="simulation-step-0002",
    )
    restored = repository.operate(
        "simulation-one",
        "tenant-one",
        "restore",
        {"snapshot_token": snapshot["snapshot_token"]},
        idempotency_key="simulation-restore-0001",
    )
    assert restored["event_hash"] == snapshot["event_hash"]
    replayed = repository.operate(
        "simulation-one",
        "tenant-one",
        "step",
        {},
        idempotency_key="simulation-step-0003",
    )
    assert {key: value for key, value in replayed.items() if key not in {"version", "etag"}} == {
        key: value for key, value in future.items() if key not in {"version", "etag"}
    }
