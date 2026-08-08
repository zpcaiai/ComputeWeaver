from __future__ import annotations

from enum import IntEnum


class RiskLevel(IntEnum):
    L0 = 0
    L1 = 1
    L2 = 2
    L3 = 3
    L4 = 4


def classify(
    *,
    external_write: bool,
    energy_action: bool,
    reversible: bool,
    critical_service_impact: bool,
    safety_boundary_change: bool,
) -> RiskLevel:
    if safety_boundary_change:
        return RiskLevel.L4
    if critical_service_impact or (energy_action and not reversible):
        return RiskLevel.L3
    if external_write or energy_action:
        return RiskLevel.L2
    if not reversible:
        return RiskLevel.L1
    return RiskLevel.L0
