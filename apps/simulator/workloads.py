from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class SimulatedWorkload:
    id: str
    gpu_count: int
    runtime_steps: int
    power_kw_per_gpu: Decimal
    urgent: bool = False


class WorkloadArrivalModel:
    def __init__(self, seed: int, arrival_probability: float = 0.12) -> None:
        if not 0 <= arrival_probability <= 1:
            raise ValueError("arrival probability must be in [0,1]")
        self.random = random.Random(seed)  # noqa: S311 - deterministic simulation, not security
        self.arrival_probability = arrival_probability
        self.sequence = 0

    def next(self, *, burst: int = 0, urgent: bool = False) -> tuple[SimulatedWorkload, ...]:
        count = (1 if self.random.random() < self.arrival_probability else 0) + burst + int(urgent)
        output: list[SimulatedWorkload] = []
        for _ in range(count):
            self.sequence += 1
            output.append(
                SimulatedWorkload(
                    f"sim-job-{self.sequence}",
                    1 if urgent else 2,
                    1 if urgent else self.random.randint(2, 8),
                    Decimal("0.6"),
                    urgent,
                )
            )
        return tuple(output)
