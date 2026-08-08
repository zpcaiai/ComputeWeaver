from __future__ import annotations

from dataclasses import dataclass

from .workloads import SimulatedWorkload


@dataclass(frozen=True, slots=True)
class AllocationState:
    running: tuple[SimulatedWorkload, ...]
    queued: tuple[SimulatedWorkload, ...]
    available_gpus: int


class SimulatedCluster:
    def __init__(self, gpu_count: int) -> None:
        if gpu_count < 1:
            raise ValueError("simulated cluster requires GPUs")
        self.gpu_count = gpu_count
        self.failed_gpus = 0
        self._running: list[tuple[SimulatedWorkload, int]] = []
        self._queued: list[SimulatedWorkload] = []

    def submit(self, workloads: tuple[SimulatedWorkload, ...]) -> None:
        self._queued.extend(workloads)
        self._schedule()

    def fail_gpus(self, count: int) -> None:
        if count < 0 or count > self.gpu_count:
            raise ValueError("invalid simulated GPU failure count")
        self.failed_gpus = count

    def step(self) -> tuple[str, ...]:
        completed: list[str] = []
        remaining: list[tuple[SimulatedWorkload, int]] = []
        for job, steps in self._running:
            if steps <= 1:
                completed.append(job.id)
            else:
                remaining.append((job, steps - 1))
        self._running = remaining
        self._schedule()
        return tuple(completed)

    def snapshot(self) -> AllocationState:
        used = sum(job.gpu_count for job, _ in self._running)
        return AllocationState(
            tuple(job for job, _ in self._running),
            tuple(self._queued),
            max(0, self.gpu_count - self.failed_gpus - used),
        )

    def _schedule(self) -> None:
        used = sum(job.gpu_count for job, _ in self._running)
        available = self.gpu_count - self.failed_gpus - used
        pending: list[SimulatedWorkload] = []
        for job in self._queued:
            if job.gpu_count <= available:
                self._running.append((job, job.runtime_steps))
                available -= job.gpu_count
            else:
                pending.append(job)
        self._queued = pending
