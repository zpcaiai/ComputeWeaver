from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import httpx

from packages.compute.inventory import ComputeNode, Gpu, NodeState
from packages.compute.snapshot import ComputeSnapshot, SnapshotBuilder
from packages.connectors.compute.base import ComputeAdapter, ReadOnlyAdapter


class SlurmAdapter(ComputeAdapter, ReadOnlyAdapter):
    """Read-only Slurm inventory adapter using slurmrestd JSON APIs."""

    def __init__(
        self,
        name: str,
        nodes: tuple[ComputeNode, ...] = (),
        *,
        api_url: str | None = None,
        jwt_token: str | None = None,
        user_name: str | None = None,
        ca_bundle: str | bool = True,
        timeout_seconds: float = 10,
        tenant_id: str | None = None,
        site_id: str | None = None,
        api_version: str = "v0.0.42",
    ) -> None:
        self.name = name
        self.nodes = nodes
        self.api_url = api_url.rstrip("/") if api_url else None
        self.jwt_token = jwt_token
        self.user_name = user_name
        self.ca_bundle = ca_bundle
        self.timeout_seconds = timeout_seconds
        self.tenant_id = tenant_id
        self.site_id = site_id
        self.api_version = api_version

    def _client(self) -> httpx.Client:
        if not self.api_url or not self.api_url.startswith("https://"):
            raise ValueError("slurmrestd API URL must use HTTPS")
        if not self.jwt_token:
            raise PermissionError("Slurm JWT token is unavailable")
        headers = {"X-SLURM-USER-TOKEN": self.jwt_token, "Accept": "application/json"}
        if self.user_name:
            headers["X-SLURM-USER-NAME"] = self.user_name
        return httpx.Client(
            base_url=self.api_url,
            headers=headers,
            timeout=self.timeout_seconds,
            verify=self.ca_bundle,
            follow_redirects=False,
        )

    def discover(self) -> tuple[ComputeNode, ...]:
        if not self.api_url:
            return self.nodes
        with self._client() as client:
            response = client.get(f"/slurm/{self.api_version}/nodes")
            response.raise_for_status()
            document = response.json()
        raw_nodes = document.get("nodes") if isinstance(document, dict) else None
        if not isinstance(raw_nodes, list):
            raise ConnectionError("Slurm node response is malformed")
        return tuple(self._normalize_node(item) for item in raw_nodes if isinstance(item, dict))

    def _normalize_node(self, item: dict[str, Any]) -> ComputeNode:
        node_id = str(item.get("name") or item.get("node_name") or "")
        if not node_id:
            raise ValueError("Slurm node has no name")
        tenant_id = self.tenant_id or str(item.get("tenant_id") or "")
        site_id = self.site_id or str(item.get("site_id") or item.get("cluster") or "")
        if not tenant_id or not site_id:
            raise ValueError("Slurm node is missing tenant/site mapping")
        gres = item.get("gres") or item.get("gres_used") or ""
        gpu_count, gpu_model = self._parse_gres(gres)
        gpu_memory = Decimal(str(item.get("gpu_memory_gb", 0)))
        gpu_power = Decimal(str(item.get("gpu_max_power_kw", 0)))
        gpus = tuple(Gpu(f"{node_id}/gpu-{index}", gpu_model, gpu_memory, gpu_power) for index in range(gpu_count))
        raw_state = item.get("state") or item.get("state_flags") or []
        state_text = " ".join(str(value) for value in raw_state) if isinstance(raw_state, list) else str(raw_state)
        if any(flag in state_text.upper() for flag in ("DOWN", "FAIL", "NOT_RESPONDING")):
            state = NodeState.FAILED
        elif any(flag in state_text.upper() for flag in ("DRAIN", "MAINT")):
            state = NodeState.MAINTENANCE
        else:
            state = NodeState.READY
        return ComputeNode(
            id=node_id,
            tenant_id=tenant_id,
            site_id=site_id,
            topology_asset_id=str(item.get("topology_asset_id") or node_id),
            gpus=gpus,
            cpu_cores=int(item.get("cpus") or item.get("cpus_effective") or 0),
            memory_gb=Decimal(str(item.get("real_memory") or 0)) / Decimal(1024),
            state=state,
            labels={"slurm.partition": str(item.get("partitions") or "")},
        )

    @staticmethod
    def _parse_gres(raw: object) -> tuple[int, str]:
        entries = raw if isinstance(raw, list) else str(raw).split(",")
        for entry in entries:
            text = str(entry)
            if not text.startswith("gpu:"):
                continue
            parts = text.split(":")
            try:
                if len(parts) >= 3:
                    return int(parts[2].split("(", 1)[0]), parts[1]
                return int(parts[1].split("(", 1)[0]), "unknown"
            except ValueError:
                continue
        return 0, "unknown"

    def snapshot(self, tenant_id: str, topology_version: int) -> ComputeSnapshot:
        return SnapshotBuilder().build(
            tenant_id=tenant_id,
            topology_version=topology_version,
            source=self.name,
            nodes=self.discover(),
        )

    def watch(self) -> Iterator[ComputeSnapshot]:
        nodes = self.discover()
        tenant_id = nodes[0].tenant_id if nodes else (self.tenant_id or "unknown")
        yield SnapshotBuilder().build(
            tenant_id=tenant_id,
            topology_version=1,
            source=self.name,
            nodes=nodes,
        )

    def validate_credentials(self) -> bool:
        if not self.api_url:
            return bool(self.nodes)
        try:
            with self._client() as client:
                response = client.get(f"/slurm/{self.api_version}/ping")
                return response.status_code == 200
        except (httpx.HTTPError, PermissionError, ValueError):
            return False

    def dry_run(self, action: dict[str, object]) -> dict[str, object]:
        return {"adapter": self.name, "read_only": True, "would_apply": dict(action)}
