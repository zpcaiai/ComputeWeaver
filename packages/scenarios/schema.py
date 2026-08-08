from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ALLOWED_FAULTS = frozenset(
    {
        "high_price",
        "pv_surplus",
        "job_burst",
        "urgent_job",
        "pv_error",
        "battery_unavailable",
        "gpu_failure",
        "grid_derating",
        "island_mode",
    }
)


@dataclass(frozen=True, slots=True)
class FaultSpec:
    step: int
    kind: str
    target: str


@dataclass(frozen=True, slots=True)
class ScenarioDocument:
    name: str
    version: str
    seed: int
    duration_hours: int
    faults: tuple[FaultSpec, ...]


def validate_document(document: dict[str, Any]) -> ScenarioDocument:
    required = {"name", "version", "seed", "duration_hours"}
    missing = required - set(document)
    if missing:
        raise ValueError(f"scenario fields missing: {sorted(missing)}")
    if str(document["version"]) != "1.0.0":
        raise ValueError("unsupported scenario version")
    duration = int(document["duration_hours"])
    if duration < 1 or duration > 168:
        raise ValueError("scenario duration must be between 1 and 168 hours")
    raw_faults = document.get("faults", [])
    if not isinstance(raw_faults, list):
        raise ValueError("scenario faults must be a list")
    faults = tuple(
        FaultSpec(int(item["step"]), str(item["kind"]), str(item.get("target", "site"))) for item in raw_faults
    )
    if any(item.step < 0 for item in faults):
        raise ValueError("fault step cannot be negative")
    unsupported = {item.kind for item in faults} - ALLOWED_FAULTS
    if unsupported:
        raise ValueError(f"unsupported scenario faults: {sorted(unsupported)}")
    return ScenarioDocument(
        str(document["name"]),
        str(document["version"]),
        int(document["seed"]),
        duration,
        faults,
    )
