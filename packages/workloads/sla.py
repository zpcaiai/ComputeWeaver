from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .models import Sla


@dataclass(frozen=True, slots=True)
class SlaObservation:
    observed_at: datetime
    completed_at: datetime | None = None
    latency_ms: int | None = None
    availability: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SlaEvaluation:
    met: bool
    violations: tuple[str, ...]
    at_risk: bool


def evaluate_sla(sla: Sla, observation: SlaObservation, *, risk_window_seconds: int = 900) -> SlaEvaluation:
    if observation.observed_at.tzinfo is None or sla.deadline.tzinfo is None:
        raise ValueError("SLA timestamps must be timezone-aware")
    if risk_window_seconds < 0:
        raise ValueError("risk window cannot be negative")
    violations: list[str] = []
    if observation.completed_at is not None and observation.completed_at > sla.deadline:
        violations.append("DEADLINE_MISSED")
    if sla.max_latency_ms is not None and observation.latency_ms is not None:
        if observation.latency_ms > sla.max_latency_ms:
            violations.append("LATENCY_EXCEEDED")
    at_risk = (
        observation.completed_at is None
        and (sla.deadline - observation.observed_at).total_seconds() <= risk_window_seconds
    )
    return SlaEvaluation(not violations, tuple(violations), at_risk)
