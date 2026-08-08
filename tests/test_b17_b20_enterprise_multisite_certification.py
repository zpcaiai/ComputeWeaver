from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from packages.admin.versioned_config import VersionedConfig
from packages.budgets.service import Budget, BudgetLedger
from packages.certification.service import GateResult, certify_release, verify_certificate
from packages.chargeback.service import allocate_cost
from packages.dr.service import backup_state, reconcile_restore, verify_backup
from packages.iam.service import Identity, Policy, authorize
from packages.island.planner import plan_island_survival
from packages.multisite.model import Site, SiteLink
from packages.multisite.optimizer import evaluate_migration
from packages.notifications.service import NotificationRouter, Route
from packages.reports.service import build_savings_report, grounded_narrative
from packages.resilience.planner import CriticalLoad
from packages.scheduling.strategies import schedule_fifo, schedule_price_aware
from packages.sovereignty.service import ResidencyPolicy, enforce_residency


def test_savings_report_is_reconciled_and_grounded(schedule_input) -> None:
    baseline = schedule_fifo(schedule_input)
    candidate = schedule_price_aware(schedule_input)
    report = build_savings_report(baseline, candidate, tariff_version="tariff-one@1", run_id="run-one")
    assert report.savings == baseline.estimated_cost - candidate.estimated_cost
    narrative = grounded_narrative(report)
    assert str(report.savings) in narrative
    assert "run-one" not in narrative  # narrative contains only the declared operational values.


def test_tenant_auth_budget_chargeback_notifications_and_config(now) -> None:
    identity = Identity(
        "user-one",
        "tenant-one",
        frozenset({"operator"}),
        {"site": "site-one"},
        now + timedelta(hours=1),
    )
    policy = Policy("read", frozenset({"operator"}), {"site": "site-one"})
    assert authorize(identity, policy, resource_tenant="tenant-one", now=now)
    with pytest.raises(PermissionError, match="cross-tenant"):
        authorize(identity, policy, resource_tenant="tenant-two", now=now)
    ledger = BudgetLedger()
    ledger.configure(Budget("tenant-one", Decimal(100), Decimal(100), Decimal(50), Decimal(100)))
    assert not ledger.record("tenant-one", cost=Decimal(10), carbon_kg=Decimal(5), gpu_hours=Decimal(5)).warning
    assert ledger.record("tenant-one", cost=Decimal(75), carbon_kg=Decimal(0), gpu_hours=Decimal(0)).warning
    allocations = allocate_cost(Decimal("10.01"), {("project", "a"): Decimal(1), ("project", "b"): Decimal(2)})
    assert sum((item.amount for item in allocations), Decimal(0)) == Decimal("10.01")
    router = NotificationRouter()
    router.add_route(Route("BudgetThresholdReached", "email", "ops", 2, timedelta(hours=1)))
    assert len(router.route(event_id="evt-one", event_type="BudgetThresholdReached", severity=3, now=now)) == 1
    assert router.route(event_id="evt-two", event_type="BudgetThresholdReached", severity=3, now=now) == ()
    config = VersionedConfig(frozenset({"mode"}))
    first = config.update({"mode": "shadow"}, "admin-one", now)
    config.update({"mode": "guarded"}, "admin-one", now)
    assert config.rollback(first.version, "admin-two", now).values["mode"] == "shadow"


def test_multisite_sovereignty_migration_and_island_reserve(sample_jobs) -> None:
    job = sample_jobs[0]
    source = Site("site-one", "cn-east", 8, Decimal(100), Decimal("0.4"), Decimal("0.5"))
    destination = Site("site-two", "cn-east", 8, Decimal(100), Decimal("0.1"), Decimal("0.2"))
    link = SiteLink("site-one", "site-two", Decimal(10), Decimal(5), Decimal("0.001"))
    rejected = evaluate_migration(
        job, source, destination, link, checkpoint_size_gb=Decimal(100), remaining_hours=Decimal(2)
    )
    assert not rejected.allowed
    assert "SOVEREIGNTY" in rejected.reasons
    enforce_residency(ResidencyPolicy("private", frozenset({"cn-east"})), "cn-east", "cn-east")
    with pytest.raises(PermissionError):
        enforce_residency(ResidencyPolicy("private", frozenset({"cn-east"})), "cn-east", "us-west")
    plan = plan_island_survival(
        (
            CriticalLoad("inference", Decimal(10), 100, True),
            CriticalLoad("batch", Decimal(30), 10, False),
        ),
        battery_kwh=Decimal(100),
        reserved_kwh=Decimal(20),
        generator_kwh=Decimal(0),
        pv_kw=Decimal(0),
    )
    assert plan.reserve_kwh == Decimal(20)
    assert "inference" in plan.served


def test_backup_restore_reconciliation_and_certification_fail_closed(now) -> None:
    backup = backup_state({"action": "safe", "version": 1}, now)
    assert verify_backup(backup)
    assert reconcile_restore(backup.state, dict(backup.state)).safe
    result = certify_release(
        release_id="candidate-one",
        commit="abc123",
        generated_at=now,
        gate_results=(
            GateResult("build", True, ("build.log",)),
            GateResult("tests", True, ("tests.xml",)),
            GateResult("contracts", True, ("contracts.json",)),
        ),
    )
    assert result.status == "NOT_CERTIFIED"
    assert verify_certificate(result)
    assert any(gate.name == "security" and not gate.passed for gate in result.gates)
