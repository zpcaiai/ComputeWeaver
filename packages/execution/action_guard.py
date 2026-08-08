from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from packages.approval.workflow import ApprovalRequest, ApprovalStatus
from packages.risk.classifier import RiskLevel


@dataclass(frozen=True, slots=True)
class Action:
    id: str
    plan_id: str
    tenant_id: str
    target: str
    kind: str
    expected_state_version: str
    parameters: dict[str, Any]
    bounds: dict[str, tuple[Decimal, Decimal]]
    timeout_seconds: int
    idempotency_key: str
    risk: RiskLevel
    created_at: datetime
    compensation_kind: str | None


@dataclass(frozen=True, slots=True)
class GuardDecision:
    allowed: bool
    reasons: tuple[str, ...]


class ActionGuard:
    PROHIBITED_ENERGY_COMMANDS = frozenset({"breaker", "relay", "pcs_switch", "firmware"})

    def __init__(self, whitelist: frozenset[str], max_age: timedelta = timedelta(minutes=5)) -> None:
        self.whitelist = whitelist
        self.max_age = max_age

    def evaluate(
        self,
        action: Action,
        *,
        current_state_version: str,
        now: datetime,
        approval: ApprovalRequest | None,
    ) -> GuardDecision:
        reasons: list[str] = []
        if action.kind not in self.whitelist:
            reasons.append("ACTION_NOT_WHITELISTED")
        if action.kind in self.PROHIBITED_ENERGY_COMMANDS:
            reasons.append("PROHIBITED_LOW_LEVEL_ENERGY_COMMAND")
        if current_state_version != action.expected_state_version:
            reasons.append("STALE_TARGET_STATE")
        if now - action.created_at > self.max_age:
            reasons.append("STALE_ACTION")
        for name, (minimum, maximum) in action.bounds.items():
            try:
                value = Decimal(str(action.parameters[name]))
            except (KeyError, ValueError):
                reasons.append(f"BOUND_VALUE_MISSING:{name}")
                continue
            if not minimum <= value <= maximum:
                reasons.append(f"BOUND_VIOLATION:{name}")
        if action.risk >= RiskLevel.L2:
            if approval is None or approval.status != ApprovalStatus.APPROVED:
                reasons.append("VALID_APPROVAL_REQUIRED")
            elif approval.plan_id != action.plan_id or approval.tenant_id != action.tenant_id:
                reasons.append("APPROVAL_SCOPE_MISMATCH")
            elif now >= approval.expires_at:
                reasons.append("APPROVAL_EXPIRED")
        return GuardDecision(not reasons, tuple(reasons))
