from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

os.environ.setdefault("COMPUTEWEAVER_ENV", "test")
os.environ.setdefault("COMPUTEWEAVER_DATABASE_URL", "memory://")
os.environ.setdefault("COMPUTEWEAVER_AUTH_MODE", "trusted_headers")

from packages.scheduling.contracts import ScheduleInput, TimeSlot
from packages.workloads.models import Job, ResourceRequest, Sla, WorkloadClass


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def sample_jobs(now: datetime) -> tuple[Job, ...]:
    def make(job_id: str, submitted: int, priority: int, checkpointable: bool = False) -> Job:
        return Job(
            id=job_id,
            tenant_id="tenant-one",
            project_id="project-one",
            workload_class=WorkloadClass.TRAINING,
            request=ResourceRequest(2, "H100", 8, Decimal(64), Decimal(1), Decimal("0.6")),
            sla=Sla(now + timedelta(hours=4), priority),
            submitted_at=now + timedelta(minutes=submitted),
            allowed_sites=frozenset({"site-one"}),
            checkpointable=checkpointable,
        )

    return make("job-one", 0, 40), make("job-two", 1, 90)


@pytest.fixture
def schedule_input(now: datetime, sample_jobs: tuple[Job, ...]) -> ScheduleInput:
    slots = tuple(
        TimeSlot(
            index=index,
            starts_at=now + timedelta(hours=index),
            duration_hours=Decimal(1),
            gpu_capacity=2,
            power_capacity_kw=Decimal(10),
            price_per_kwh=Decimal(price),
        )
        for index, price in enumerate(("0.30", "0.10", "0.20", "0.40"))
    )
    return ScheduleInput(sample_jobs, slots, 1, "forecast-v1", "fifo")
