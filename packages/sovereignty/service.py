from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResidencyPolicy:
    data_class: str
    allowed_regions: frozenset[str]
    export_controlled: bool = False


def enforce_residency(policy: ResidencyPolicy, source_region: str, destination_region: str) -> None:
    if destination_region not in policy.allowed_regions:
        raise PermissionError("data residency violation")
    if policy.export_controlled and source_region != destination_region:
        raise PermissionError("export-controlled data cannot cross regions")
