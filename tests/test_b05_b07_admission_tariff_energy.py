from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest

from packages.admission.service import AdmissionService
from packages.compute.inventory import ComputeNode, Gpu
from packages.compute.snapshot import SnapshotBuilder
from packages.energy.assets import Battery, GridConnection
from packages.energy.battery import next_soc
from packages.energy.power_balance import Dispatch, validate_power_balance
from packages.tariffs.calculator import MeterInterval, TariffCalculator, validate_tariff_versions
from packages.tariffs.models import PricePeriod, TariffPlan
from packages.workloads.models import Job, ResourceRequest, Sla, WorkloadClass
from packages.workloads.quota import Quota, QuotaLedger
from packages.workloads.state_machine import JobState, transition, validate_dependency_graph


def test_job_state_machine_and_dependency_cycles(sample_jobs: tuple[Job, ...]) -> None:
    assert transition(JobState.SUBMITTED, JobState.ADMITTED) == JobState.ADMITTED
    with pytest.raises(ValueError):
        transition(JobState.SUBMITTED, JobState.SUCCEEDED)
    cyclic = (
        replace(sample_jobs[0], dependencies=frozenset({"job-two"})),
        replace(sample_jobs[1], dependencies=frozenset({"job-one"})),
    )
    with pytest.raises(ValueError, match="cycle"):
        validate_dependency_graph(cyclic)


def test_admission_enforces_capacity_quota_and_deadline(now: datetime) -> None:
    node = ComputeNode(
        "node-one",
        "tenant-one",
        "site-one",
        "node-one",
        (Gpu("gpu-one", "H100", Decimal(80), Decimal("0.7")),),
        32,
        Decimal(256),
    )
    snapshot = SnapshotBuilder().build(tenant_id="tenant-one", topology_version=1, source="sim", nodes=(node,), now=now)
    ledger = QuotaLedger()
    ledger.configure("tenant-one", Quota(1, Decimal(10), 1))
    job = Job(
        "job-one",
        "tenant-one",
        "project-one",
        WorkloadClass.TRAINING,
        ResourceRequest(1, "H100", 4, Decimal(32), Decimal(1), Decimal("0.6")),
        Sla(now + timedelta(hours=2), 50),
        now,
        frozenset({"site-one"}),
    )
    result = AdmissionService(ledger).evaluate(job, snapshot, now)
    assert result.status == "admitted"
    second = AdmissionService(ledger).evaluate(replace(job, id="job-two"), snapshot, now)
    assert second.status == "rejected"
    assert "QUOTA" in second.blocking_constraints


def tariff() -> TariffPlan:
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


def test_tariff_golden_bill_and_version_overlap() -> None:
    intervals = (
        MeterInterval(datetime(2026, 1, 2, 10, tzinfo=UTC), Decimal(1), Decimal(10), peak_kw=Decimal(5)),
        MeterInterval(datetime(2026, 1, 2, 20, tzinfo=UTC), Decimal(1), Decimal(10), peak_kw=Decimal(7)),
    )
    result = TariffCalculator().calculate(tariff(), intervals)
    # 05:00 local off-peak + 15:00 local peak = 1 + 3; demand 70; tax 7.4.
    assert result.energy.amount == Decimal("4.00")
    assert result.demand.amount == Decimal("70.00")
    assert result.total.amount == Decimal("81.40")
    second = replace(tariff(), version=2, effective_from=date(2026, 2, 1))
    with pytest.raises(ValueError, match="overlapping"):
        validate_tariff_versions((tariff(), second))


def battery() -> Battery:
    return Battery(
        "battery-one",
        "power-zone-one",
        Decimal(100),
        Decimal(20),
        Decimal(20),
        Decimal("0.9"),
        Decimal("0.9"),
        Decimal("0.1"),
        Decimal("0.9"),
        Decimal("0.2"),
    )


def test_battery_soc_and_power_balance_constraints() -> None:
    assert next_soc(
        battery(),
        Decimal("0.5"),
        charge_kw=Decimal(10),
        discharge_kw=Decimal(0),
        duration_hours=Decimal(1),
    ) == Decimal("0.59")
    with pytest.raises(ValueError, match="simultaneously"):
        next_soc(
            battery(),
            Decimal("0.5"),
            charge_kw=Decimal(1),
            discharge_kw=Decimal(1),
            duration_hours=Decimal(1),
        )
    result = validate_power_balance(
        compute_load_kw=Decimal(50),
        pue=Decimal("1.2"),
        fixed_load_kw=Decimal(10),
        dispatch=Dispatch(Decimal(50), Decimal(0), Decimal(20), Decimal(0), Decimal(0)),
        grid=GridConnection("grid-one", "zone-one", Decimal(100)),
    )
    assert result.residual_kw == 0
    assert result.violations == ()
    violated = validate_power_balance(
        compute_load_kw=Decimal(50),
        pue=Decimal("1.2"),
        fixed_load_kw=Decimal(10),
        dispatch=Dispatch(Decimal(101), Decimal(0), Decimal(0), Decimal(0), Decimal(0)),
        grid=GridConnection("grid-one", "zone-one", Decimal(100)),
    )
    assert "GRID_IMPORT_CAPACITY" in violated.violations
