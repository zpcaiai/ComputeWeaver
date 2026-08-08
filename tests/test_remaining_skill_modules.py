from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest

from packages.carbon.calculator import calculate_carbon
from packages.constraints.catalog import Constraint, ConstraintCatalog
from packages.energy.state import EnergyState, StateKind
from packages.explain.counterfactual import change_slot_prices
from packages.explain.reason_codes import ReasonCode
from packages.faults.operators import FaultOperator
from packages.optimization.diagnostics import diagnose_infeasibility
from packages.plans.diff import compare
from packages.region_packs.base import StandardRegionPack
from packages.scheduling.contracts import Allocation, ScheduleInput, SchedulePlan
from packages.simulation.engine import SimulationConfig, Simulator
from packages.simulation.sdk import SimulationSdk
from packages.tariffs.advanced import (
    EnergyTier,
    RealTimePrice,
    calculate_tiered_energy,
    real_time_price_at,
    subsidy_credit,
)
from packages.tariffs.calculator import MeterInterval
from packages.tariffs.calendar import BillingCalendar, local_billing_date, normalize_local
from packages.tariffs.models import PricePeriod, TariffPlan
from packages.topology.importers import import_csv
from packages.whatif.service import run_what_if


def test_carbon_accounting_and_governed_constraint_catalog() -> None:
    result = calculate_carbon(Decimal("100"), Decimal("0.45"), Decimal("0.08"), "USD")
    assert result.emissions.kg_co2e == Decimal("45.00")
    assert result.carbon_cost.amount == Decimal("3.60")
    with pytest.raises(ValueError, match="negative"):
        calculate_carbon(Decimal("-1"), Decimal("0.45"), Decimal("0.08"))

    catalog = ConstraintCatalog()
    capacity = Constraint("grid-cap", "hard", "energy-ops", "admission", "site grid limit")
    catalog.register(capacity)
    catalog.register(capacity)
    catalog.register(Constraint("carbon-cap", "soft", "sustainability", "optimizer", "carbon budget"))
    assert [item.id for item in catalog.all()] == ["carbon-cap", "grid-cap"]
    assert catalog.require("grid-cap") == capacity
    with pytest.raises(ValueError, match="redefined"):
        catalog.register(Constraint("grid-cap", "soft", "other", "report", "changed"))
    with pytest.raises(ValueError, match="unknown"):
        catalog.require("missing")


def test_energy_state_and_reason_code_are_explicitly_typed(now: datetime) -> None:
    state = EnergyState(
        "site-one",
        now,
        StateKind.ACTUAL,
        Decimal("80"),
        Decimal("20"),
        Decimal("0.55"),
        Decimal("100"),
        "meter-v1",
    )
    assert state.kind.value == "actual"
    assert ReasonCode.BATTERY_RESERVE.value == "BATTERY_RESERVE"
    assert {item.value for item in StateKind} == {"actual", "forecast", "planned"}


def test_fault_operator_is_non_mutating_and_validates_fault_state() -> None:
    original: dict[str, Any] = {"power_kw": 100, "faults": [{"kind": "existing"}]}
    changed = FaultOperator("derating", "grid", 0.25).apply(original)
    faults = changed["faults"]
    assert isinstance(faults, list)
    assert len(faults) == 2
    assert len(original["faults"]) == 1
    assert FaultOperator("outage", "battery").apply({})["faults"][0]["magnitude"] == 1.0
    with pytest.raises(ValueError, match="list"):
        FaultOperator("outage", "battery").apply({"faults": "invalid"})


def _plan(
    *,
    allocations: tuple[Allocation, ...],
    cost: str,
    energy: str,
    input_hash: str = "input-hash",
) -> SchedulePlan:
    return SchedulePlan(
        "test",
        allocations,
        (),
        Decimal(energy),
        Decimal(cost),
        (),
        (),
        input_hash,
    )


def test_plan_diff_tracks_add_remove_change_cost_and_energy() -> None:
    left = _plan(
        allocations=(
            Allocation("job-one", (0,), 2, "EARLIEST_FEASIBLE"),
            Allocation("job-removed", (1,), 1, "EARLIEST_FEASIBLE"),
        ),
        cost="10",
        energy="20",
    )
    right = _plan(
        allocations=(
            Allocation("job-one", (2,), 2, "MIN_TOTAL_COST"),
            Allocation("job-added", (1,), 1, "MIN_TOTAL_COST"),
        ),
        cost="8",
        energy="18",
    )
    difference = compare(left, right)
    assert difference.added_jobs == frozenset({"job-added"})
    assert difference.removed_jobs == frozenset({"job-removed"})
    assert difference.changed_jobs == frozenset({"job-one"})
    assert difference.cost_delta == Decimal("-2")
    assert difference.energy_delta_kwh == Decimal("-2")


def _tariff(*, tax_rate: str = "0.1") -> TariffPlan:
    return TariffPlan(
        "tariff-one",
        1,
        "USD",
        "Asia/Shanghai",
        date(2026, 1, 1),
        None,
        (PricePeriod("all-day", time(0), time(0), Decimal("0.5")),),
        demand_charge_per_kw=Decimal("2"),
        capacity_charge_per_kw=Decimal("1"),
        tax_rate=Decimal(tax_rate),
        feed_in_price_per_kwh=Decimal("0.1"),
    )


def test_region_pack_validates_calculates_and_explains_tariff() -> None:
    pack = StandardRegionPack("CN")
    tariff = _tariff()
    assert pack.normalize(tariff) is tariff
    result = pack.calculate(
        tariff,
        (
            MeterInterval(
                datetime(2026, 1, 1, tzinfo=UTC),
                Decimal("1"),
                Decimal("10"),
                export_kwh=Decimal("2"),
                peak_kw=Decimal("4"),
            ),
        ),
    )
    assert result.energy.amount == Decimal("5.00")
    assert result.demand.amount == Decimal("8.00")
    assert pack.explain(result)[-1].startswith("total=")
    with pytest.raises(ValueError, match="100%"):
        pack.validate(_tariff(tax_rate="1.01"))


def test_billing_calendar_handles_local_dates_and_naive_input() -> None:
    moment = datetime(2025, 12, 31, 17, tzinfo=UTC)
    assert local_billing_date(moment, "Asia/Shanghai") == date(2026, 1, 1)
    assert normalize_local(datetime(2026, 1, 1, 8), "Asia/Shanghai") == datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone-aware"):
        local_billing_date(datetime(2026, 1, 1), "UTC")
    calendar = BillingCalendar("Asia/Shanghai", frozenset({date(2026, 1, 5)}))
    assert calendar.next_business_day(date(2026, 1, 2)) == date(2026, 1, 6)


def test_tiered_realtime_and_subsidy_tariff_rules() -> None:
    tiers = (
        EnergyTier(Decimal("100"), Decimal("0.10")),
        EnergyTier(Decimal("200"), Decimal("0.20")),
        EnergyTier(None, Decimal("0.30")),
    )
    assert calculate_tiered_energy(Decimal("250"), tiers, "USD").amount == Decimal("45.00")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    prices = (
        RealTimePrice(now - timedelta(minutes=10), Decimal("0.15")),
        RealTimePrice(now - timedelta(minutes=2), Decimal("0.25")),
    )
    assert real_time_price_at(prices, now, maximum_age=timedelta(minutes=5)) == Decimal("0.25")
    assert subsidy_credit(Decimal("50"), Decimal("0.02"), "USD").amount == Decimal("1.00")
    with pytest.raises(LookupError, match="stale"):
        real_time_price_at(prices, now + timedelta(hours=1), maximum_age=timedelta(minutes=5))


def test_topology_csv_import_reports_duplicates_and_bad_rows() -> None:
    result = import_csv(
        "id,site_id,kind,name,capacity_kw\n"
        "site-one,site-one,site,Primary,100\n"
        "rack-one,site-one,rack,Rack 1,50\n"
        "rack-one,site-one,rack,Duplicate,50\n"
        "bad,site-one,not-a-kind,Bad,10\n",
        tenant_id="tenant-one",
    )
    assert [asset.id for asset in result.assets] == ["site-one", "rack-one"]
    assert result.assets[0].tenant_id == "tenant-one"
    assert result.conflicts[0] == "row 4: duplicate id"
    assert "not-a-kind" in result.conflicts[1]


def test_simulation_sdk_dispatches_commands_and_keeps_restore_deterministic() -> None:
    simulator = Simulator(SimulationConfig(duration_hours=1, step_minutes=15, seed=9))
    sdk = SimulationSdk(simulator)
    before = sdk.command("snapshot")
    first = sdk.command("step", fault="urgent_job")
    assert first["urgent_job"] is True
    assert sdk.observe()["step"] == 1
    sdk.command("restore", snapshot=before)
    assert sdk.observe()["step"] == 0
    events = sdk.command("run", fault_schedule={0: "grid_derating"})
    assert len(events) == 4
    assert events[0]["grid_limit_kw"] == 20.0
    with pytest.raises(ValueError, match="unsupported"):
        sdk.command("delete")


def test_infeasibility_counterfactual_and_what_if(
    schedule_input: ScheduleInput,
) -> None:
    expensive = change_slot_prices(schedule_input, Decimal("2"))
    assert expensive.parameter == "price_multiplier"
    assert expensive.changed == Decimal("2")
    assert expensive.objective_delta is not None
    scaled = run_what_if(schedule_input, capacity_multiplier=Decimal("2"))
    assert scaled.isolated is True
    assert scaled.changed_parameters == {"capacity_multiplier": "2"}
    assert scaled.result.status == "optimal"
    assert schedule_input.slots[0].gpu_capacity == 2
    with pytest.raises(ValueError, match="positive"):
        change_slot_prices(schedule_input, Decimal(0))
    with pytest.raises(ValueError, match="positive"):
        run_what_if(schedule_input, capacity_multiplier=Decimal(0))

    no_slots = ScheduleInput(
        schedule_input.jobs,
        (),
        schedule_input.topology_version,
        schedule_input.forecast_version,
    )
    issues = diagnose_infeasibility(no_slots)
    assert "NO_TIME_SLOTS" in issues
    assert all(f"GPU_CAPACITY:{job.id}" in issues for job in schedule_input.jobs)
    assert all(f"DEADLINE:{job.id}" in issues for job in schedule_input.jobs)
