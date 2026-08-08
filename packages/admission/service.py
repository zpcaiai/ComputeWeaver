from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from packages.compute.snapshot import ComputeSnapshot
from packages.workloads.models import Job, WorkloadClass
from packages.workloads.quota import QuotaLedger


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    status: str
    blocking_constraints: tuple[str, ...]
    capacity_shortfall: int
    earliest_feasible_window: datetime | None


class AdmissionService:
    def __init__(self, quota: QuotaLedger) -> None:
        self.quota = quota

    def evaluate(self, job: Job, snapshot: ComputeSnapshot, now: datetime) -> AdmissionResult:
        blocking: list[str] = []
        if snapshot.tenant_id != job.tenant_id:
            blocking.append("TENANT_SCOPE")
        if snapshot.quality in {"stale", "conflict"}:
            blocking.append("COMPUTE_DATA_QUALITY")
        available = snapshot.schedulable_gpu_count
        shortfall = max(0, job.request.gpu_count - available)
        if shortfall:
            blocking.append("GPU_CAPACITY")
        completion = now + timedelta(hours=float(job.request.estimated_hours))
        if completion > job.sla.deadline:
            blocking.append("DEADLINE")
        if job.allowed_sites and not any(node.site_id in job.allowed_sites for node in snapshot.nodes):
            blocking.append("SOVEREIGNTY")
        if job.workload_class != WorkloadClass.ONLINE_INFERENCE:
            protected = sum(len(item.gpu_ids) for item in snapshot.reservations if item.protected)
            if available - job.request.gpu_count < protected:
                blocking.append("PROTECTED_INFERENCE_RESERVATION")
        gpu_hours = Decimal(job.request.gpu_count) * job.request.estimated_hours
        if not blocking and not self.quota.reserve(
            job.tenant_id,
            job.request.gpu_count,
            gpu_hours,
            reservation_key=job.id,
        ):
            blocking.append("QUOTA")
        return AdmissionResult(
            status="admitted" if not blocking else "rejected",
            blocking_constraints=tuple(blocking),
            capacity_shortfall=shortfall,
            earliest_feasible_window=None if not blocking else now + timedelta(hours=1),
        )
