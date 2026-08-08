from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from packages.optimization.engine import OptimizationResult


@dataclass(frozen=True, slots=True)
class Explanation:
    primary_reason: str
    supporting_factors: tuple[str, ...]
    conflicting_factors: tuple[str, ...]
    binding_constraints: tuple[str, ...]
    objective_impact: dict[str, Decimal]
    uncertainty: tuple[str, ...]
    provenance: dict[str, str]


def explain_plan(
    result: OptimizationResult,
    *,
    input_hash: str,
    model_version: str,
    forecast_quality: Decimal,
) -> Explanation:
    if result.plan is None:
        return Explanation(
            "INFEASIBLE",
            (),
            result.diagnostics,
            result.diagnostics,
            result.objective_breakdown,
            (),
            {"input_hash": input_hash, "model_version": model_version, "solver": result.solver},
        )
    reasons = tuple(sorted({item.reason_code for item in result.plan.allocations}))
    uncertainty = (f"forecast_quality={forecast_quality}",) if forecast_quality < Decimal("0.9") else ()
    return Explanation(
        reasons[0] if reasons else "NO_ACTION",
        reasons[1:],
        (),
        result.diagnostics,
        result.objective_breakdown,
        uncertainty,
        {"input_hash": input_hash, "model_version": model_version, "solver": result.solver},
    )


def binding_constraints(slacks: dict[str, Decimal], tolerance: Decimal) -> tuple[str, ...]:
    return tuple(sorted(name for name, slack in slacks.items() if abs(slack) <= tolerance))
