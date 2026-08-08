from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class DependencyJob(Protocol):
    id: str
    dependencies: frozenset[str]


class JobState(StrEnum):
    SUBMITTED = "submitted"
    ADMITTED = "admitted"
    REJECTED = "rejected"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    COMPENSATING = "compensating"


TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.SUBMITTED: frozenset({JobState.ADMITTED, JobState.REJECTED, JobState.CANCELLED}),
    JobState.ADMITTED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.REJECTED: frozenset(),
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELLED, JobState.FAILED}),
    JobState.RUNNING: frozenset({JobState.PAUSED, JobState.CANCELLING, JobState.SUCCEEDED, JobState.FAILED}),
    JobState.PAUSED: frozenset({JobState.RUNNING, JobState.CANCELLED, JobState.FAILED}),
    JobState.CANCELLING: frozenset({JobState.CANCELLED, JobState.COMPENSATING}),
    JobState.COMPENSATING: frozenset({JobState.CANCELLED, JobState.FAILED}),
    JobState.CANCELLED: frozenset(),
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset(),
}


def transition(current: JobState, target: JobState) -> JobState:
    if target not in TRANSITIONS[current]:
        raise ValueError(f"invalid job transition {current} -> {target}")
    return target


def validate_dependency_graph(jobs: tuple[DependencyJob, ...]) -> None:
    by_id = {job.id: job for job in jobs}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(job_id: str) -> None:
        if job_id in visiting:
            raise ValueError("job dependency cycle")
        if job_id in visited:
            return
        if job_id not in by_id:
            raise ValueError(f"unknown dependency {job_id}")
        visiting.add(job_id)
        for dependency in by_id[job_id].dependencies:
            visit(dependency)
        visiting.remove(job_id)
        visited.add(job_id)

    for item in by_id:
        visit(item)
