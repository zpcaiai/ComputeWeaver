from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from packages.approval.workflow import ApprovalRequest, ApprovalStatus, ApprovalWorkflow
from packages.execution.action_guard import Action, ActionGuard
from packages.execution.adapters import SimulatorExecutor
from packages.execution.compensation import Operation, execute_transaction
from packages.execution.idempotency import IdempotencyStore
from packages.plans.lifecycle import GovernedPlan, PlanLifecycle, PlanState
from packages.policy.engine import PolicyEngine
from packages.policy.models import Enforcement, Policy, PolicyRule
from packages.risk.classifier import RiskLevel, classify


def test_policy_conflicts_and_hard_policy_role() -> None:
    engine = PolicyEngine()
    first = Policy(
        "policy-one",
        1,
        "tenant-one",
        frozenset({"site-one"}),
        PolicyRule("region", "eq", "cn-east"),
        Enforcement.HARD,
        100,
        "safety",
    )
    with pytest.raises(PermissionError):
        engine.publish(first, frozenset({"admin"}))
    engine.publish(first, frozenset({"safety_admin"}))
    conflict = Policy(
        "policy-two",
        1,
        "tenant-one",
        frozenset({"site-one"}),
        PolicyRule("region", "eq", "us-west"),
        Enforcement.HARD,
        100,
        "safety",
    )
    with pytest.raises(ValueError, match="conflict"):
        engine.publish(conflict, frozenset({"safety_admin"}))
    decision = engine.evaluate("tenant-one", "site-one", {"region": "us-west"})
    assert not decision.allowed
    assert decision.hard_violations == ("policy-one",)


def test_plan_lifecycle_optimistic_lock_and_stale_state(now) -> None:
    lifecycle = PlanLifecycle()
    plan = GovernedPlan(
        "plan-one",
        "tenant-one",
        "site-one",
        1,
        PlanState.DRAFT,
        "state-v1",
        ("policy-one@1",),
        RiskLevel.L2,
        required_approvers=1,
    )
    lifecycle.create(plan)
    lifecycle.transition("plan-one", PlanState.VALIDATED, expected_version=1)
    with pytest.raises(RuntimeError, match="optimistic"):
        lifecycle.transition("plan-one", PlanState.APPROVED, expected_version=1)
    lifecycle.transition("plan-one", PlanState.APPROVED, expected_version=2)
    with pytest.raises(ValueError, match="stale"):
        lifecycle.transition(
            "plan-one",
            PlanState.ACTIVE,
            expected_version=3,
            current_state_version="state-v2",
            at=now,
        )
    active = lifecycle.transition(
        "plan-one", PlanState.ACTIVE, expected_version=3, current_state_version="state-v1", at=now
    )
    assert active.state == PlanState.ACTIVE


def approved_request(now) -> ApprovalRequest:
    workflow = ApprovalWorkflow()
    request = ApprovalRequest(
        "approval-one",
        "plan-one",
        "tenant-one",
        RiskLevel.L3,
        "requester",
        now + timedelta(hours=1),
        frozenset({"operator", "safety"}),
        2,
    )
    workflow.create(request)
    workflow.approve("approval-one", actor_id="operator-one", role="operator", now=now)
    return workflow.approve("approval-one", actor_id="safety-one", role="safety", now=now)


def test_dual_approval_action_guard_bounds_freshness_and_prohibited_commands(now) -> None:
    approval = approved_request(now)
    assert approval.status == ApprovalStatus.APPROVED
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
    assert guard.evaluate(action, current_state_version="state-v1", now=now, approval=approval).allowed
    prohibited = replace(action, kind="breaker")
    decision = guard.evaluate(prohibited, current_state_version="state-v1", now=now, approval=approval)
    assert "PROHIBITED_LOW_LEVEL_ENERGY_COMMAND" in decision.reasons


def test_idempotent_execution_and_compensation() -> None:
    executor = SimulatorExecutor()
    idempotency = IdempotencyStore()
    calls = 0

    def execute() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return executor.execute("schedule", {"job": "job-one"})

    first = idempotency.execute_once("idem-12345678", {"job": "job-one"}, execute)
    second = idempotency.execute_once("idem-12345678", {"job": "job-one"}, execute)
    assert first == second
    assert calls == 1
    state = {"value": 0}

    def apply_one() -> dict[str, object]:
        previous = state["value"]
        state["value"] = 1
        return {"previous": previous}

    def compensate_one(evidence: dict[str, object]) -> dict[str, object]:
        state["value"] = int(evidence["previous"])
        return {"value": state["value"]}

    def fail() -> dict[str, object]:
        raise OSError("downstream failed")

    with pytest.raises(OSError):
        execute_transaction((Operation(apply_one, compensate_one), Operation(fail, lambda _: {})))
    assert state["value"] == 0


def test_risk_classifier_is_monotonic() -> None:
    assert (
        classify(
            external_write=False,
            energy_action=False,
            reversible=True,
            critical_service_impact=False,
            safety_boundary_change=False,
        )
        == RiskLevel.L0
    )
    assert (
        classify(
            external_write=True,
            energy_action=True,
            reversible=False,
            critical_service_impact=True,
            safety_boundary_change=False,
        )
        == RiskLevel.L3
    )
