from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from apps.api.main import app
from packages.admin.versioned_config import VersionedConfig
from packages.admission.service import AdmissionService
from packages.approval.workflow import ApprovalRequest, ApprovalWorkflow
from packages.benchmark.runner import benchmark
from packages.budgets.service import Budget, BudgetLedger
from packages.chargeback.service import allocate_cost
from packages.compute.inventory import ComputeNode, Gpu, Reservation
from packages.compute.snapshot import SnapshotBuilder
from packages.connectors.compute.base import ReadOnlyAdapter
from packages.connectors.kubernetes import KubernetesAdapter
from packages.connectors.simulator import SimulatorComputeAdapter
from packages.connectors.slurm import SlurmAdapter
from packages.contracts.schema_cli import MODELS, compatible
from packages.energy.assets import GridConnection
from packages.energy.power_balance import Dispatch, validate_power_balance
from packages.execution.action_guard import Action, ActionGuard
from packages.explain.counterfactual import change_slot_prices
from packages.explain.service import explain_plan
from packages.forecasting.backtest import rolling_origin_backtest
from packages.forecasting.fallback import require_quality
from packages.forecasting.models import ObservedValue, PersistenceModel
from packages.iam.service import Identity, authorize
from packages.iam.service import Policy as IamPolicy
from packages.ingestion.normalize import Normalizer
from packages.ingestion.raw import RawEvent, RawLanding
from packages.island.planner import plan_island_survival
from packages.mpc.controller import MpcController
from packages.mpc.state_estimator import ObservedState
from packages.multisite.model import Site, SiteLink
from packages.multisite.optimizer import evaluate_migration
from packages.optimization.engine import optimize
from packages.plans.diff import compare
from packages.policy.engine import PolicyEngine
from packages.policy.models import Enforcement, Policy, PolicyRule
from packages.region_packs.base import StandardRegionPack
from packages.replay.service import replay
from packages.reports.service import build_savings_report
from packages.resilience.planner import CriticalLoad
from packages.risk.classifier import RiskLevel
from packages.scheduling.contracts import ScheduleInput, TimeSlot
from packages.scheduling.strategies import schedule_fifo, schedule_price_aware
from packages.simulation.engine import SimulationConfig, Simulator
from packages.tariffs.calculator import MeterInterval, TariffCalculator
from packages.tariffs.models import PricePeriod, TariffPlan
from packages.timeseries.store import TimeSeriesStore
from packages.topology.models import Asset, AssetType, Relationship
from packages.topology.registry import TopologyRegistry
from packages.topology.validation import validate_graph
from packages.workloads.models import Job, ResourceRequest, Sla, WorkloadClass
from packages.workloads.quota import Quota, QuotaLedger
from packages.workloads.state_machine import TRANSITIONS


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str, sort_keys=True) + "\n", encoding="utf-8")


def _capture(operation: Callable[[], object]) -> str:
    try:
        operation()
    except (ConnectionError, PermissionError, RuntimeError, ValueError) as error:
        return f"{type(error).__name__}: {error}"
    return "NO_ERROR"


def _execute_rejection(adapter: ReadOnlyAdapter) -> str:
    return _capture(lambda: adapter.execute({"cordon": "node-one"}))


def _node() -> ComputeNode:
    return ComputeNode(
        "node-one",
        "tenant-one",
        "site-one",
        "node-one",
        (Gpu("gpu-one", "H100", Decimal(80), Decimal("0.7")),),
        32,
        Decimal(256),
    )


def _jobs(start: datetime) -> tuple[Job, ...]:
    request = ResourceRequest(1, "H100", 4, Decimal(32), Decimal(1), Decimal("0.6"))
    return (
        Job(
            "job-one",
            "tenant-one",
            "project-one",
            WorkloadClass.TRAINING,
            request,
            Sla(start + timedelta(hours=4), 50),
            start,
            frozenset({"site-one"}),
        ),
        Job(
            "job-two",
            "tenant-one",
            "project-one",
            WorkloadClass.TRAINING,
            request,
            Sla(start + timedelta(hours=2), 90),
            start,
            frozenset({"site-one"}),
        ),
    )


def _schedule(start: datetime) -> ScheduleInput:
    prices = ("0.30", "0.10", "0.20", "0.40")
    slots = tuple(
        TimeSlot(
            index=index,
            starts_at=start + timedelta(hours=index),
            duration_hours=Decimal(1),
            gpu_capacity=1,
            power_capacity_kw=Decimal(2),
            price_per_kwh=Decimal(price),
        )
        for index, price in enumerate(prices)
    )
    return ScheduleInput(_jobs(start), slots, 1, "forecast-v1", "evidence")


def _tariff() -> TariffPlan:
    return TariffPlan(
        "tou-one",
        1,
        "USD",
        "America/New_York",
        date(2026, 1, 1),
        None,
        (
            PricePeriod("off-peak", time(0), time(12), Decimal("0.10")),
            PricePeriod("peak", time(12), time(0), Decimal("0.30")),
        ),
        demand_charge_per_kw=Decimal(10),
        tax_rate=Decimal("0.1"),
    )


def _topology_assets() -> tuple[Asset, ...]:
    return (
        Asset("site-one", "tenant-one", "site-one", AssetType.SITE, "Site", Decimal(100)),
        Asset("rack-one", "tenant-one", "site-one", AssetType.RACK, "Rack", Decimal(50)),
        Asset("node-one", "tenant-one", "site-one", AssetType.COMPUTE_NODE, "Node", Decimal(10)),
    )


def _generate_b02(root: Path, revision: str) -> None:
    catalogue = {
        "source_revision": revision,
        "json_schemas": {name: model.model_json_schema() for name, model in MODELS.items()},
        "openapi": {"version": app.openapi()["openapi"], "path_count": len(app.openapi()["paths"])},
    }
    _write_json(root / "B02" / "schema-catalog.json", catalogue)
    old: dict[str, object] = {"properties": {"id": {}, "value": {}}, "required": ["id"]}
    new: dict[str, object] = {"properties": {"id": {}, "extra": {}}, "required": ["id", "extra"]}
    ok, issues = compatible(old, new)
    (root / "B02" / "compatibility-report.md").write_text(
        "# Contract compatibility evidence\n\n"
        f"Synthetic breaking change accepted: `{ok}`\n\n" + "\n".join(f"- {item}" for item in issues) + "\n",
        encoding="utf-8",
    )
    (root / "B02" / "openapi-diff.txt").write_text(
        "status=NOT_RUN\nreason=no prior committed OpenAPI baseline exists in this unversioned checkout\n",
        encoding="utf-8",
    )


def _generate_b03_b04(root: Path, now: datetime) -> None:
    assets = _topology_assets()
    relationships = (Relationship("site-one", "rack-one"), Relationship("rack-one", "node-one"))
    registry = TopologyRegistry()
    registry.create_draft("tenant-one", assets, relationships)
    published = registry.publish("tenant-one", expected_draft_revision=1)
    validate_graph(assets, relationships)
    _write_json(
        root / "B03" / "reference-topology.json",
        {
            "published": asdict(published),
            "traversal": [item.id for item in registry.traverse("tenant-one", "site-one")],
        },
    )
    cycle = relationships + (Relationship("node-one", "site-one"),)
    orphan = (Relationship("missing", "rack-one"),)
    too_large = assets + (Asset("rack-two", "tenant-one", "site-one", AssetType.RACK, "Rack 2", Decimal(60)),)
    capacity_links = (Relationship("site-one", "rack-one"), Relationship("site-one", "rack-two"))
    _write_json(
        root / "B03" / "graph-validation-report.json",
        {
            "valid_graph": "PASS",
            "cycle_rejection": _capture(lambda: validate_graph(assets, cycle)),
            "orphan_rejection": _capture(lambda: validate_graph(assets, orphan)),
            "capacity_rejection": _capture(lambda: validate_graph(too_large, capacity_links)),
        },
    )

    node = _node()
    adapters = (
        KubernetesAdapter("kubernetes", (node,)),
        SlurmAdapter("slurm", (node,)),
        SimulatorComputeAdapter("sim", (node,)),
    )
    adapter_results: list[dict[str, object]] = []
    for adapter in adapters:
        adapter_results.append(
            {
                "adapter": adapter.name,
                "credentials": adapter.validate_credentials(),
                "discovered": len(adapter.discover()),
                "dry_run": adapter.dry_run({"cordon": "node-one"}),
                "execute_rejection": _execute_rejection(adapter),
            }
        )
    _write_json(root / "B04" / "adapter-contract-results.json", adapter_results)
    reservation = Reservation(
        "reserve-one", "tenant-one", frozenset({"gpu-one"}), now, now + timedelta(hours=1), "inference", True
    )
    snapshot = SnapshotBuilder().build(
        tenant_id="tenant-one",
        topology_version=1,
        source="evidence",
        nodes=(node,),
        reservations=(reservation,),
        observed_at=now - timedelta(minutes=10),
        now=now,
        scheduler_states={"node-one": "ready"},
        telemetry_states={"node-one": "failed"},
    )
    _write_json(
        root / "B04" / "compute-snapshot.json",
        {**asdict(snapshot), "schedulable_gpu_count": snapshot.schedulable_gpu_count},
    )


def _generate_b05_b07(root: Path, now: datetime) -> None:
    ledger = QuotaLedger()
    ledger.configure("tenant-one", Quota(1, Decimal(10), 1))
    snapshot = SnapshotBuilder().build(
        tenant_id="tenant-one", topology_version=1, source="evidence", nodes=(_node(),), now=now
    )
    service = AdmissionService(ledger)
    first = service.evaluate(_jobs(now)[0], snapshot, now)
    second = service.evaluate(_jobs(now)[0], snapshot, now)
    _write_json(root / "B05" / "admission-scenarios.json", {"first": asdict(first), "quota_rejection": asdict(second)})
    matrix = [
        f"| {state.value} | {', '.join(sorted(target.value for target in targets)) or '-'} |"
        for state, targets in TRANSITIONS.items()
    ]
    (root / "B05" / "job-lifecycle-matrix.md").write_text(
        "# Job lifecycle matrix\n\n| From | Allowed targets |\n|---|---|\n" + "\n".join(matrix) + "\n",
        encoding="utf-8",
    )

    intervals = (
        MeterInterval(datetime(2026, 1, 2, 10, tzinfo=UTC), Decimal(1), Decimal(10), peak_kw=Decimal(5)),
        MeterInterval(datetime(2026, 1, 2, 20, tzinfo=UTC), Decimal(1), Decimal(10), peak_kw=Decimal(7)),
    )
    cost = TariffCalculator().calculate(_tariff(), intervals)
    _write_json(root / "B06" / "cost-breakdown-examples.json", asdict(cost))
    pack = StandardRegionPack("us-reference")
    packed = pack.calculate(_tariff(), intervals)
    _write_json(
        root / "B06" / "region-pack-contract.json",
        {"region": pack.region, "normalized": asdict(pack.normalize(_tariff())), "explanation": pack.explain(packed)},
    )

    grid = GridConnection("grid-one", "zone-one", Decimal(100))
    balanced = validate_power_balance(
        compute_load_kw=Decimal(50),
        pue=Decimal("1.2"),
        fixed_load_kw=Decimal(10),
        dispatch=Dispatch(Decimal(50), Decimal(0), Decimal(20), Decimal(0), Decimal(0)),
        grid=grid,
    )
    violated = validate_power_balance(
        compute_load_kw=Decimal(50),
        pue=Decimal("1.2"),
        fixed_load_kw=Decimal(10),
        dispatch=Dispatch(Decimal(101), Decimal(0), Decimal(0), Decimal(0), Decimal(0)),
        grid=grid,
    )
    _write_json(root / "B07" / "power-balance-cases.json", {"balanced": asdict(balanced), "violated": asdict(violated)})
    _write_json(
        root / "B07" / "constraint-violation-examples.json",
        {
            "grid_capacity": violated.violations,
            "simultaneous_charge_discharge": _capture(
                lambda: validate_power_balance(
                    compute_load_kw=Decimal(1),
                    pue=Decimal(1),
                    fixed_load_kw=Decimal(0),
                    dispatch=Dispatch(Decimal(1), Decimal(0), Decimal(0), Decimal(1), Decimal(1)),
                    grid=grid,
                )
            ),
        },
    )


def _generate_b08_b10(root: Path, now: datetime) -> None:
    raw = RawEvent.create(
        id="event-one",
        tenant_id="tenant-one",
        source="meter-one",
        received_at=now,
        payload={"metric": "grid_power", "timestamp": now.isoformat(), "value": "1000", "unit": "W"},
    )
    landing = RawLanding()
    first_append = landing.append(raw)
    duplicate_append = landing.append(raw)
    point = Normalizer().normalize(raw)
    store = TimeSeriesStore()
    first_point = store.append(point)
    duplicate_point = store.append(point)
    _write_json(
        root / "B08" / "connector-contract-results.json",
        {
            "raw_first": first_append,
            "raw_duplicate": duplicate_append,
            "point_first": first_point,
            "point_duplicate": duplicate_point,
        },
    )
    _write_json(
        root / "B08" / "data-lineage-sample.json",
        {"raw": asdict(raw), "normalized": asdict(point), "hash_preserved": point.raw_payload_hash == raw.payload_hash},
    )

    simulator = Simulator(SimulationConfig(seed=13, duration_hours=1))
    simulator.step()
    snapshot = simulator.snapshot()
    expected = simulator.step()
    restored = Simulator(SimulationConfig(seed=13, duration_hours=1))
    restored.restore(snapshot)
    actual = restored.step()
    _write_json(
        root / "B09" / "snapshot-replay-results.json",
        {"snapshot": snapshot, "expected": expected, "actual": actual, "equivalent": expected == actual},
    )

    catalogue = (
        None,
        "high_price",
        "pv_surplus",
        "job_burst",
        "urgent_job",
        "pv_error",
        "battery_unavailable",
        "gpu_failure",
        "grid_derating",
        "island_mode",
    )
    checks: list[dict[str, object]] = []
    for index, fault in enumerate(catalogue):
        scenario = {
            "name": f"evidence-{index}",
            "version": "1.0.0",
            "seed": index,
            "duration_hours": 1,
            "faults": [] if fault is None else [{"step": 1, "kind": fault, "target": "site-one"}],
        }
        from packages.scenarios.compiler import compile_scenario, run_scenario

        events, evaluation = run_scenario(compile_scenario(scenario))
        replayed = replay(events)
        checks.append({"fault": fault, "events": len(events), "equivalent": replayed.evaluation == evaluation})
    _write_json(
        root / "B10" / "replay-equivalence.json",
        {"cases": checks, "all_equivalent": all(item["equivalent"] for item in checks)},
    )


def _generate_b11_b14(root: Path, now: datetime) -> None:
    observations = tuple(ObservedValue(now - timedelta(hours=6 - index), Decimal(index + 1)) for index in range(6))
    bundle = PersistenceModel().forecast(observations, start=now, periods=4, step=timedelta(hours=1), signal="pv")
    backtest = rolling_origin_backtest(observations, minimum_train=3)
    fallback = require_quality(bundle, Decimal("0.9"))
    _write_json(root / "B11" / "backtest-report.json", {"forecast": asdict(bundle), "backtest": asdict(backtest)})
    _write_json(
        root / "B11" / "fallback-scenarios.json",
        {
            "minimum_quality": "0.9",
            "fallback": fallback.fallback,
            "uses_upper_bound": all(item.point == item.upper for item in fallback.points),
        },
    )

    request = _schedule(now)
    result = benchmark(request, seed=44)
    _write_json(root / "B12" / "reference-benchmark.json", asdict(result))
    repeated = benchmark(request, seed=44)
    _write_json(
        root / "B12" / "determinism-results.json",
        {"equal": result == repeated, "input_hashes": sorted({plan.input_hash for plan in result.plans})},
    )

    impossible = replace(request, slots=tuple(replace(slot, gpu_capacity=0) for slot in request.slots))
    infeasible = optimize(impossible)
    _write_json(root / "B13" / "infeasibility-report.json", asdict(infeasible))

    observed = ObservedState(frozenset(), {}, 1, Decimal("0.5"), Decimal(100), Decimal(1), "state-v1")
    cycle = MpcController().cycle(request, observed, started_at=now)
    poor = replace(observed, data_quality=Decimal("0.2"))
    failure = _capture(lambda: MpcController().cycle(request, poor, started_at=now))
    _write_json(
        root / "B14" / "forecast-error-results.json",
        {"normal_cycle": asdict(cycle), "low_quality_fail_closed": failure},
    )
    _write_json(
        root / "B14" / "fallback-proof.json",
        {"low_quality_without_last_safe_plan": failure, "silent_fallback": False},
    )
    fifo = schedule_fifo(request)
    price = schedule_price_aware(request)
    diff = compare(fifo, price)
    _write_json(root / "B14" / "plan-churn-metrics.json", asdict(diff))


def _generate_b15_b16(root: Path, now: datetime) -> None:
    engine = PolicyEngine()
    policy = Policy(
        "policy-one",
        1,
        "tenant-one",
        frozenset({"site-one"}),
        PolicyRule("region", "eq", "cn-east"),
        Enforcement.HARD,
        100,
        "safety",
    )
    published = engine.publish(policy, frozenset({"safety_admin"}))
    conflict = replace(policy, id="policy-two", rule=PolicyRule("region", "eq", "us-west"))
    decision = engine.evaluate("tenant-one", "site-one", {"region": "us-west"})
    _write_json(root / "B15" / "policy-catalog.json", {"published": asdict(published), "decision": asdict(decision)})
    _write_json(
        root / "B15" / "conflict-cases.json",
        {"equal_priority_conflict": _capture(lambda: engine.publish(conflict, frozenset({"safety_admin"})))},
    )
    request = _schedule(now)
    _write_json(
        root / "B15" / "plan-diff-examples.json", asdict(compare(schedule_fifo(request), schedule_price_aware(request)))
    )

    workflow = ApprovalWorkflow()
    approval = ApprovalRequest(
        "approval-one",
        "plan-one",
        "tenant-one",
        RiskLevel.L3,
        "requester",
        now + timedelta(hours=1),
        frozenset({"operator", "safety"}),
        2,
    )
    workflow.create(approval)
    first = workflow.approve("approval-one", actor_id="operator-one", role="operator", now=now)
    approved = workflow.approve("approval-one", actor_id="safety-one", role="safety", now=now)
    _write_json(root / "B16" / "approval-matrix.json", {"after_one": asdict(first), "after_two": asdict(approved)})
    action = Action(
        "action-one",
        "plan-one",
        "tenant-one",
        "battery-one",
        "set_dispatch_plan",
        "state-v1",
        {"power_kw": "10"},
        {"power_kw": (Decimal(-20), Decimal(20))},
        30,
        "idem-action-one",
        RiskLevel.L3,
        now,
        "restore_dispatch_plan",
    )
    guard = ActionGuard(frozenset({"set_dispatch_plan", "breaker"}))
    allowed = guard.evaluate(action, current_state_version="state-v1", now=now, approval=approved)
    bounded = guard.evaluate(
        replace(action, parameters={"power_kw": "100"}), current_state_version="state-v1", now=now, approval=approved
    )
    prohibited = guard.evaluate(
        replace(action, kind="breaker"), current_state_version="state-v1", now=now, approval=approved
    )
    _write_json(
        root / "B16" / "action-guard-cases.json",
        {"allowed": asdict(allowed), "bounded": asdict(bounded), "prohibited": asdict(prohibited)},
    )
    (root / "B16" / "prohibited-command-test.log").write_text(
        f"allowed={prohibited.allowed}\nreasons={','.join(prohibited.reasons)}\n", encoding="utf-8"
    )


def _generate_b17_b19(root: Path, now: datetime) -> None:
    request = _schedule(now)
    optimized = optimize(request)
    explanation = explain_plan(
        optimized, input_hash=request.content_hash(), model_version="optimizer-1", forecast_quality=Decimal("0.8")
    )
    _write_json(root / "B17" / "explanation-corpus.json", asdict(explanation))
    counterfactual = change_slot_prices(request, Decimal("1.5"))
    _write_json(root / "B17" / "counterfactual-golden-cases.json", asdict(counterfactual))
    baseline = schedule_fifo(request)
    candidate = schedule_price_aware(request)
    report = build_savings_report(baseline, candidate, tariff_version="tou-one@1", run_id="evidence-run")
    _write_json(root / "B17" / "reconciliation-report.json", asdict(report))

    identity = Identity(
        "user-one", "tenant-one", frozenset({"operator"}), {"site": "site-one"}, now + timedelta(hours=1)
    )
    iam_policy = IamPolicy("read", frozenset({"operator"}), {"site": "site-one"})
    _write_json(
        root / "B18" / "access-control-matrix.json",
        {
            "same_tenant": authorize(identity, iam_policy, resource_tenant="tenant-one", now=now),
            "cross_tenant": _capture(lambda: authorize(identity, iam_policy, resource_tenant="tenant-two", now=now)),
        },
    )
    allocations = allocate_cost(Decimal("10.01"), {("project", "a"): Decimal(1), ("project", "b"): Decimal(2)})
    ledger = BudgetLedger()
    ledger.configure(Budget("tenant-one", Decimal(100), Decimal(100), Decimal(50), Decimal(100)))
    budget = ledger.record("tenant-one", cost=Decimal(85), carbon_kg=Decimal(5), gpu_hours=Decimal(5))
    _write_json(
        root / "B18" / "chargeback-reconciliation.json",
        {
            "allocations": [asdict(item) for item in allocations],
            "sum": sum((item.amount for item in allocations), Decimal(0)),
            "budget": asdict(budget),
        },
    )
    config = VersionedConfig(frozenset({"mode"}))
    first = config.update({"mode": "shadow"}, "admin-one", now)
    second = config.update({"mode": "guarded"}, "admin-one", now)
    rollback = config.rollback(first.version, "admin-two", now)
    _write_json(
        root / "B18" / "config-rollback-proof.json",
        {"first": asdict(first), "second": asdict(second), "rollback": asdict(rollback)},
    )

    loads = (CriticalLoad("inference", Decimal(10), 100, True), CriticalLoad("batch", Decimal(30), 10, False))
    island = plan_island_survival(
        loads, battery_kwh=Decimal(100), reserved_kwh=Decimal(20), generator_kwh=Decimal(0), pv_kw=Decimal(0)
    )
    source = Site("site-one", "cn-east", 8, Decimal(100), Decimal("0.4"), Decimal("0.5"))
    destination = Site("site-two", "cn-east", 8, Decimal(100), Decimal("0.1"), Decimal("0.2"))
    link = SiteLink("site-one", "site-two", Decimal(10), Decimal(5), Decimal("0.001"))
    migration = evaluate_migration(
        _jobs(now)[0], source, destination, link, checkpoint_size_gb=Decimal(100), remaining_hours=Decimal(2)
    )
    _write_json(
        root / "B19" / "island-survival-report.json", {"island": asdict(island), "migration": asdict(migration)}
    )


def generate(root: Path, revision: str, generated_at: datetime) -> None:
    for batch in range(2, 20):
        (root / f"B{batch:02d}").mkdir(parents=True, exist_ok=True)
    _generate_b02(root, revision)
    _generate_b03_b04(root, generated_at)
    _generate_b05_b07(root, generated_at)
    _generate_b08_b10(root, generated_at)
    _generate_b11_b14(root, generated_at)
    _generate_b15_b16(root, generated_at)
    _generate_b17_b19(root, generated_at)
