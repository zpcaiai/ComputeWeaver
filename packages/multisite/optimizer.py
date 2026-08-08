from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from packages.workloads.models import Job

from .model import Site, SiteLink


@dataclass(frozen=True, slots=True)
class MigrationDecision:
    allowed: bool
    source: str
    destination: str
    transfer_hours: Decimal | None
    compute_savings: Decimal
    transfer_cost: Decimal
    net_savings: Decimal
    reasons: tuple[str, ...]
    approval_required: bool


def evaluate_migration(
    job: Job,
    source: Site,
    destination: Site,
    link: SiteLink,
    *,
    checkpoint_size_gb: Decimal,
    remaining_hours: Decimal,
) -> MigrationDecision:
    reasons: list[str] = []
    if not source.online or not destination.online or not link.online:
        reasons.append("SITE_OR_LINK_UNAVAILABLE")
    if destination.id not in job.allowed_sites:
        reasons.append("SOVEREIGNTY")
    if destination.available_gpus < job.request.gpu_count:
        reasons.append("GPU_CAPACITY")
    if link.source != source.id or link.destination != destination.id:
        reasons.append("LINK_DIRECTION")
    transfer_hours = (
        checkpoint_size_gb * Decimal(8) / (link.bandwidth_gbps * Decimal(3600)) if link.bandwidth_gbps > 0 else None
    )
    if transfer_hours is None or transfer_hours >= remaining_hours:
        reasons.append("TRANSFER_TIME")
    compute_kwh = job.request.power_kw_per_gpu * job.request.gpu_count * remaining_hours
    savings = compute_kwh * (source.energy_price - destination.energy_price)
    transfer_cost = checkpoint_size_gb * link.transfer_cost_per_gb
    net = savings - transfer_cost
    if net <= 0:
        reasons.append("NO_NET_SAVINGS")
    return MigrationDecision(
        not reasons,
        source.id,
        destination.id,
        transfer_hours,
        savings,
        transfer_cost,
        net,
        tuple(reasons),
        True,
    )
