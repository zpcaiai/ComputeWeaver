from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from packages.approval.workflow import ApprovalRequest, ApprovalStatus, ApprovalWorkflow
from packages.execution.idempotency import IdempotencyStore
from packages.forecasting.registry import ModelRegistry, ModelStage, ModelVersion
from packages.risk.classifier import RiskLevel
from packages.workloads.quota import Quota, QuotaLedger


def _approval(
    now: datetime,
    *,
    request_id: str = "approval-one",
    risk: RiskLevel = RiskLevel.L3,
    required_count: int = 2,
    required_roles: frozenset[str] = frozenset({"safety_admin", "operations_admin"}),
) -> ApprovalRequest:
    return ApprovalRequest(
        request_id,
        "plan-one",
        "tenant-one",
        risk,
        "requester-one",
        now + timedelta(minutes=10),
        required_roles,
        required_count,
    )


def test_in_memory_approval_enforces_quorum_expiry_and_modification_rules() -> None:
    now = datetime.now(UTC)
    workflow = ApprovalWorkflow()
    with pytest.raises(ValueError, match="risk minimum"):
        workflow.create(_approval(now, required_count=1))
    with pytest.raises(ValueError, match="roles are required"):
        workflow.create(
            _approval(
                now,
                risk=RiskLevel.L2,
                required_count=1,
                required_roles=frozenset(),
            )
        )
    request = _approval(now)
    workflow.create(request)
    workflow.create(request)
    with pytest.raises(ValueError, match="different requirements"):
        workflow.create(_approval(now, required_roles=frozenset({"different"})))
    modified = workflow.modify(
        request.id,
        actor_id="requester-one",
        expires_at=now + timedelta(minutes=20),
        required_roles=request.required_roles,
        required_count=2,
    )
    assert modified.expires_at > request.expires_at
    with pytest.raises(PermissionError, match="requester"):
        workflow.approve(request.id, actor_id="requester-one", role="safety_admin", now=now)
    with pytest.raises(PermissionError, match="role"):
        workflow.approve(request.id, actor_id="person-one", role="viewer", now=now)
    pending = workflow.approve(request.id, actor_id="person-one", role="safety_admin", now=now)
    assert pending.status == ApprovalStatus.PENDING
    assert workflow.approve(request.id, actor_id="person-one", role="safety_admin", now=now) == pending
    approved = workflow.approve(request.id, actor_id="person-two", role="operations_admin", now=now)
    assert approved.status == ApprovalStatus.APPROVED
    with pytest.raises(ValueError, match="not pending"):
        workflow.reject(request.id)
    assert workflow.list("tenant-one") == (approved,)

    rejectable = _approval(now, request_id="approval-two", risk=RiskLevel.L2, required_count=1)
    workflow.create(rejectable)
    rejected = workflow.reject(rejectable.id)
    assert workflow.reject(rejectable.id) == rejected
    expired = ApprovalRequest(
        "approval-three",
        "plan-three",
        "tenant-one",
        RiskLevel.L2,
        "requester-one",
        now,
        frozenset({"safety_admin"}),
        1,
    )
    workflow.create(expired)
    with pytest.raises(ValueError, match="expired"):
        workflow.approve(expired.id, actor_id="person-three", role="safety_admin", now=now)


def test_in_memory_quota_is_idempotent_bounded_and_releasable() -> None:
    ledger = QuotaLedger()
    with pytest.raises(ValueError, match="negative"):
        ledger.configure("tenant-one", Quota(-1, Decimal(1), 1))
    assert not ledger.reserve("missing", 1, Decimal(1))
    ledger.configure("tenant-one", Quota(4, Decimal(10), 2))
    assert ledger.reserve("tenant-one", 2, Decimal(4), reservation_key="job-one")
    assert ledger.reserve("tenant-one", 2, Decimal(4), reservation_key="job-one")
    with pytest.raises(ValueError, match="different demand"):
        ledger.reserve("tenant-one", 3, Decimal(4), reservation_key="job-one")
    assert not ledger.reserve("tenant-one", 3, Decimal(1), reservation_key="job-two")
    with pytest.raises(RuntimeError, match="current usage"):
        ledger.configure("tenant-one", Quota(1, Decimal(10), 2))
    assert ledger.usage("tenant-one") == (2, Decimal(4), 1)
    ledger.release("tenant-one", 2, Decimal(4), reservation_key="job-one")
    assert ledger.usage("tenant-one") == (0, Decimal(0), 0)
    with pytest.raises(ValueError, match="negative"):
        ledger.reserve("tenant-one", -1, Decimal(0))


def test_model_registry_promotes_archives_and_rolls_back() -> None:
    now = datetime.now(UTC)
    registry = ModelRegistry()
    first = ModelVersion("demand", "v1", "a" * 64, "b" * 64, now)
    second = ModelVersion("demand", "v2", "c" * 64, "d" * 64, now + timedelta(minutes=1))
    registry.register(first)
    registry.register(second)
    with pytest.raises(ValueError, match="already exists"):
        registry.register(first)
    with pytest.raises(KeyError):
        registry.production("demand")
    assert registry.promote("demand", "v1").stage == ModelStage.PRODUCTION
    promoted = registry.promote("demand", "v2")
    assert promoted.stage == ModelStage.PRODUCTION
    rolled_back = registry.rollback("demand")
    assert rolled_back.version == "v1"
    assert registry.production("demand").version == "v1"
    isolated = ModelRegistry()
    isolated.register(first)
    isolated.promote("demand", "v1")
    with pytest.raises(ValueError, match="no previous"):
        isolated.rollback("demand")


def test_idempotency_replay_failure_and_pre_execution_cancellation() -> None:
    store = IdempotencyStore()
    calls = 0

    def operation() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"status": "accepted"}

    first = store.execute_once("key-one", {"action": "action-one", "power": 10}, operation, tenant_id="tenant-one")
    replay = store.execute_once("key-one", {"action": "action-one", "power": 10}, operation, tenant_id="tenant-one")
    assert first == replay
    assert calls == 1
    with pytest.raises(ValueError, match="different intent"):
        store.execute_once("key-one", {"action": "action-one", "power": 20}, operation, tenant_id="tenant-one")
    cancellation = store.cancel("action-two", tenant_id="tenant-one", actor_id="operator-one", reason="unsafe forecast")
    assert cancellation["status"] == "cancelled"
    assert (
        store.cancel("action-two", tenant_id="tenant-one", actor_id="operator-one", reason="unsafe forecast")
        == cancellation
    )
    with pytest.raises(PermissionError, match="cancelled"):
        store.execute_once(
            "key-two",
            {"action": "action-two"},
            operation,
            tenant_id="tenant-one",
            action_id="action-two",
        )
    with pytest.raises(ValueError, match="requires"):
        store.cancel("", tenant_id="tenant-one", actor_id="operator-one", reason="reason")

    def failure() -> None:
        raise RuntimeError("provider unavailable")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        store.execute_once("key-failure", {"action": "failure"}, failure)
