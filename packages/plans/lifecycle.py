from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from threading import RLock

from packages.risk.classifier import RiskLevel


class PlanState(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class GovernedPlan:
    id: str
    tenant_id: str
    site_id: str
    version: int
    state: PlanState
    state_version: str
    policy_versions: tuple[str, ...]
    risk: RiskLevel
    hard_violations: tuple[str, ...] = ()
    required_approvers: int = 0
    activated_at: datetime | None = None


TRANSITIONS = {
    PlanState.DRAFT: {PlanState.VALIDATED, PlanState.REJECTED},
    PlanState.VALIDATED: {PlanState.PENDING_APPROVAL, PlanState.APPROVED},
    PlanState.PENDING_APPROVAL: {PlanState.APPROVED, PlanState.REJECTED},
    PlanState.APPROVED: {PlanState.ACTIVE, PlanState.REJECTED},
    PlanState.ACTIVE: {PlanState.SUPERSEDED},
    PlanState.SUPERSEDED: set(),
    PlanState.REJECTED: set(),
}


class PlanLifecycle:
    def __init__(self) -> None:
        self._plans: dict[str, GovernedPlan] = {}
        self._lock = RLock()

    def create(self, plan: GovernedPlan) -> None:
        if plan.id in self._plans:
            raise ValueError("plan already exists")
        self._plans[plan.id] = plan

    def transition(
        self,
        plan_id: str,
        target: PlanState,
        *,
        expected_version: int,
        current_state_version: str | None = None,
        at: datetime | None = None,
    ) -> GovernedPlan:
        with self._lock:
            plan = self._plans[plan_id]
            if plan.version != expected_version:
                raise RuntimeError("optimistic lock conflict")
            if target not in TRANSITIONS[plan.state]:
                raise ValueError(f"invalid plan transition {plan.state}->{target}")
            if plan.hard_violations and target not in {PlanState.REJECTED}:
                raise ValueError("plan with hard violations cannot advance")
            if target == PlanState.ACTIVE and current_state_version != plan.state_version:
                raise ValueError("stale plan rejected after material state change")
            if target == PlanState.ACTIVE and plan.state != PlanState.APPROVED:
                raise ValueError("only approved plan can become active")
            updated = replace(
                plan,
                version=plan.version + 1,
                state=target,
                activated_at=at if target == PlanState.ACTIVE else plan.activated_at,
            )
            self._plans[plan_id] = updated
            return updated

    def get(self, plan_id: str, tenant_id: str) -> GovernedPlan:
        plan = self._plans[plan_id]
        if plan.tenant_id != tenant_id:
            raise PermissionError("cross-tenant plan access")
        return plan
