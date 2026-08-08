from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class WorkloadClass(StrEnum):
    TRAINING = "training"
    FINE_TUNING = "fine_tuning"
    RL = "rl"
    BATCH_INFERENCE = "batch_inference"
    ONLINE_INFERENCE = "online_inference"
    PREPROCESSING = "preprocessing"
    EVALUATION = "evaluation"


@dataclass(frozen=True, slots=True)
class ResourceRequest:
    gpu_count: int
    gpu_model: str | None
    cpu_cores: int
    memory_gb: Decimal
    estimated_hours: Decimal
    power_kw_per_gpu: Decimal

    def __post_init__(self) -> None:
        if self.gpu_count < 0 or self.cpu_cores < 0:
            raise ValueError("resource counts cannot be negative")
        if self.estimated_hours <= 0:
            raise ValueError("estimated runtime must be positive")


@dataclass(frozen=True, slots=True)
class Sla:
    deadline: datetime
    priority: int = 50
    max_latency_ms: int | None = None
    availability_target: Decimal | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.priority <= 100:
            raise ValueError("priority must be in [0, 100]")


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    tenant_id: str
    project_id: str
    workload_class: WorkloadClass
    request: ResourceRequest
    sla: Sla
    submitted_at: datetime
    allowed_sites: frozenset[str]
    data_regions: frozenset[str] = frozenset()
    dependencies: frozenset[str] = frozenset()
    checkpointable: bool = False
    labels: dict[str, str] = field(default_factory=dict)
