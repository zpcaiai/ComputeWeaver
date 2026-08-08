from __future__ import annotations

from dataclasses import dataclass

from packages.scheduling.contracts import SchedulePlan


@dataclass(frozen=True, slots=True)
class FallbackDecision:
    mode: str
    plan: SchedulePlan | None
    automation_allowed: bool
    reason: str


def select_fallback(
    *,
    last_safe_plan: SchedulePlan | None,
    data_quality_ok: bool,
    emergency_shedding_allowed: bool,
) -> FallbackDecision:
    if last_safe_plan is not None and data_quality_ok:
        return FallbackDecision("last_safe", last_safe_plan, True, "solver unavailable")
    if emergency_shedding_allowed:
        return FallbackDecision("emergency_shed", None, False, "manual approval required")
    return FallbackDecision("hold", None, False, "no safe automatic fallback")
