from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal

from packages.domain.time import utc_now

from .inventory import ComputeNode, NodeState, Reservation


@dataclass(frozen=True, slots=True)
class ComputeSnapshot:
    id: str
    tenant_id: str
    topology_version: int
    observed_at: datetime
    source: str
    freshness_seconds: int
    quality: str
    nodes: tuple[ComputeNode, ...]
    reservations: tuple[Reservation, ...]
    conflicts: tuple[str, ...]

    @property
    def schedulable_gpu_count(self) -> int:
        reserved = {gpu_id for item in self.reservations for gpu_id in item.gpu_ids}
        return sum(
            1 for node in self.nodes if node.state == NodeState.READY for gpu in node.gpus if gpu.id not in reserved
        )

    @property
    def schedulable_power_kw(self) -> Decimal:
        reserved = {gpu_id for item in self.reservations for gpu_id in item.gpu_ids}
        return sum(
            (
                gpu.max_power_kw
                for node in self.nodes
                if node.state == NodeState.READY
                for gpu in node.gpus
                if gpu.id not in reserved
            ),
            Decimal(0),
        )


class SnapshotBuilder:
    def build(
        self,
        *,
        tenant_id: str,
        topology_version: int,
        source: str,
        nodes: tuple[ComputeNode, ...],
        reservations: tuple[Reservation, ...] = (),
        observed_at: datetime | None = None,
        now: datetime | None = None,
        scheduler_states: dict[str, str] | None = None,
        telemetry_states: dict[str, str] | None = None,
    ) -> ComputeSnapshot:
        observed_at = observed_at or utc_now()
        now = now or utc_now()
        if any(node.tenant_id != tenant_id for node in nodes):
            raise PermissionError("snapshot contains cross-tenant node")
        all_gpu_ids = [gpu.id for node in nodes for gpu in node.gpus]
        if len(all_gpu_ids) != len(set(all_gpu_ids)):
            raise ValueError("duplicate GPU ID would double-count capacity")
        reserved_ids = [gpu_id for item in reservations for gpu_id in item.gpu_ids]
        if len(reserved_ids) != len(set(reserved_ids)):
            raise ValueError("GPU is reserved more than once")
        if not set(reserved_ids).issubset(all_gpu_ids):
            raise ValueError("reservation references unknown GPU")
        conflicts: list[str] = []
        scheduler_states = scheduler_states or {}
        telemetry_states = telemetry_states or {}
        for node_id in scheduler_states.keys() & telemetry_states.keys():
            if scheduler_states[node_id] != telemetry_states[node_id]:
                conflicts.append(node_id)
        freshness = max(0, int((now - observed_at).total_seconds()))
        quality = "conflict" if conflicts else ("stale" if freshness > 300 else "good")
        body = json.dumps([asdict(node) for node in nodes], default=str, sort_keys=True)
        digest = hashlib.sha256(body.encode()).hexdigest()[:16]
        return ComputeSnapshot(
            id=f"snapshot-{digest}",
            tenant_id=tenant_id,
            topology_version=topology_version,
            observed_at=observed_at,
            source=source,
            freshness_seconds=freshness,
            quality=quality,
            nodes=nodes,
            reservations=reservations,
            conflicts=tuple(conflicts),
        )
