from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from packages.scheduling.contracts import SchedulePlan


@dataclass(frozen=True, slots=True)
class SavingsReport:
    baseline_name: str
    baseline_cost: Decimal
    candidate_cost: Decimal
    savings: Decimal
    savings_percent: Decimal
    assumptions: tuple[str, ...]
    uncertainty: tuple[str, ...]
    provenance: dict[str, str]
    content_hash: str


def build_savings_report(
    baseline: SchedulePlan,
    candidate: SchedulePlan,
    *,
    tariff_version: str,
    run_id: str,
    uncertainty: tuple[str, ...] = (),
) -> SavingsReport:
    if baseline.strategy in {"", "none"}:
        raise ValueError("a named baseline is required")
    savings = baseline.estimated_cost - candidate.estimated_cost
    percent = savings / baseline.estimated_cost if baseline.estimated_cost else Decimal(0)
    body = {
        "baseline_name": baseline.strategy,
        "baseline_cost": str(baseline.estimated_cost),
        "candidate_cost": str(candidate.estimated_cost),
        "tariff_version": tariff_version,
        "run_id": run_id,
    }
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return SavingsReport(
        baseline.strategy,
        baseline.estimated_cost,
        candidate.estimated_cost,
        savings,
        percent,
        tuple(candidate.assumptions),
        uncertainty,
        {
            "baseline_input_hash": baseline.input_hash,
            "candidate_input_hash": candidate.input_hash,
            "tariff_version": tariff_version,
            "run_id": run_id,
        },
        digest,
    )


def grounded_narrative(report: SavingsReport) -> str:
    return (
        f"Against {report.baseline_name}, the candidate changes estimated cost from "
        f"{report.baseline_cost} to {report.candidate_cost}, a difference of {report.savings}. "
        f"Assumptions: {', '.join(report.assumptions) or 'none recorded'}."
    )
