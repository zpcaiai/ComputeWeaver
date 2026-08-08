from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class NodeState(StrEnum):
    READY = "ready"
    MAINTENANCE = "maintenance"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MigSlice:
    id: str
    memory_gb: Decimal
    compute_fraction: Decimal

    def __post_init__(self) -> None:
        if not 0 < self.compute_fraction <= 1:
            raise ValueError("MIG compute fraction must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class Gpu:
    id: str
    model: str
    memory_gb: Decimal
    max_power_kw: Decimal
    mig_slices: tuple[MigSlice, ...] = ()

    def __post_init__(self) -> None:
        if sum((item.compute_fraction for item in self.mig_slices), Decimal(0)) > 1:
            raise ValueError("MIG slices over-allocate GPU compute")


@dataclass(frozen=True, slots=True)
class ComputeNode:
    id: str
    tenant_id: str
    site_id: str
    topology_asset_id: str
    gpus: tuple[Gpu, ...]
    cpu_cores: int
    memory_gb: Decimal
    state: NodeState = NodeState.READY
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Reservation:
    id: str
    tenant_id: str
    gpu_ids: frozenset[str]
    starts_at: datetime
    ends_at: datetime
    purpose: str
    protected: bool = False

    def __post_init__(self) -> None:
        if self.ends_at <= self.starts_at:
            raise ValueError("reservation must end after it starts")
