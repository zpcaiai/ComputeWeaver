from __future__ import annotations

import os
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from packages.compute.inventory import ComputeNode, Gpu, NodeState
from packages.compute.snapshot import ComputeSnapshot, SnapshotBuilder
from packages.connectors.compute.base import ComputeAdapter, ReadOnlyAdapter


class KubernetesAdapter(ComputeAdapter, ReadOnlyAdapter):
    """Read-only Kubernetes inventory adapter using the Kubernetes HTTPS API."""

    def __init__(
        self,
        name: str,
        nodes: tuple[ComputeNode, ...] = (),
        *,
        api_url: str | None = None,
        token: str | None = None,
        token_file: str | None = None,
        ca_bundle: str | bool | None = None,
        timeout_seconds: float = 10,
        tenant_id: str | None = None,
        site_id: str | None = None,
    ) -> None:
        self.name = name
        self.nodes = nodes
        self.api_url = api_url or os.getenv("KUBERNETES_SERVICE_HOST") and self._in_cluster_url()
        self.token = token
        self.token_file = token_file or "/var/run/secrets/kubernetes.io/serviceaccount/token"
        self.ca_bundle = ca_bundle if ca_bundle is not None else "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        self.timeout_seconds = timeout_seconds
        self.tenant_id = tenant_id
        self.site_id = site_id

    @staticmethod
    def _in_cluster_url() -> str:
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        return f"https://{host}:{port}"

    def _authorization(self) -> str:
        token = self.token
        if not token and Path(self.token_file).is_file():
            token = Path(self.token_file).read_text(encoding="utf-8").strip()
        if not token:
            raise PermissionError("Kubernetes service account token is unavailable")
        return f"Bearer {token}"

    def _client(self) -> httpx.Client:
        if not self.api_url or not self.api_url.startswith("https://"):
            raise ValueError("Kubernetes API URL must use HTTPS")
        verify: str | bool = (
            self.ca_bundle if isinstance(self.ca_bundle, bool) or Path(self.ca_bundle).is_file() else True
        )
        return httpx.Client(
            base_url=self.api_url,
            headers={"Authorization": self._authorization(), "Accept": "application/json"},
            timeout=self.timeout_seconds,
            verify=verify,
            follow_redirects=False,
        )

    def discover(self) -> tuple[ComputeNode, ...]:
        if not self.api_url:
            return self.nodes
        items: list[dict[str, Any]] = []
        continuation: str | None = None
        with self._client() as client:
            while True:
                params = {"limit": "500"}
                if continuation:
                    params["continue"] = continuation
                response = client.get("/api/v1/nodes", params=params)
                response.raise_for_status()
                document = response.json()
                if not isinstance(document, dict) or not isinstance(document.get("items"), list):
                    raise ConnectionError("Kubernetes node response is malformed")
                items.extend(item for item in document["items"] if isinstance(item, dict))
                metadata = document.get("metadata", {})
                continuation = metadata.get("continue") if isinstance(metadata, dict) else None
                if not continuation:
                    break
        return tuple(self._normalize_node(item) for item in items)

    def _normalize_node(self, item: dict[str, Any]) -> ComputeNode:
        metadata = item.get("metadata", {})
        status = item.get("status", {})
        labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
        capacity = status.get("capacity", {}) if isinstance(status, dict) else {}
        if not isinstance(labels, dict) or not isinstance(capacity, dict):
            raise ValueError("Kubernetes node metadata is malformed")
        node_id = str(metadata.get("uid") or metadata.get("name"))
        if not node_id or node_id == "None":
            raise ValueError("Kubernetes node has no stable identifier")
        tenant_id = self.tenant_id or str(labels.get("computeweaver.io/tenant", ""))
        site_id = self.site_id or str(labels.get("topology.kubernetes.io/region", ""))
        if not tenant_id or not site_id:
            raise ValueError("Kubernetes node is missing tenant/site mapping labels")
        gpu_count = int(capacity.get("nvidia.com/gpu", 0))
        gpu_model = str(labels.get("nvidia.com/gpu.product") or labels.get("nvidia.com/gpu.machine") or "unknown")
        gpu_memory = Decimal(str(labels.get("computeweaver.io/gpu-memory-gb", "0")))
        gpu_power = Decimal(str(labels.get("computeweaver.io/gpu-max-power-kw", "0")))
        gpus = tuple(Gpu(f"{node_id}/gpu-{index}", gpu_model, gpu_memory, gpu_power) for index in range(gpu_count))
        conditions = status.get("conditions", []) if isinstance(status, dict) else []
        ready = any(
            isinstance(condition, dict) and condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in conditions
        )
        cpu = str(capacity.get("cpu", "0"))
        cpu_cores = int(cpu[:-1]) // 1000 if cpu.endswith("m") else int(Decimal(cpu))
        memory = str(capacity.get("memory", "0"))
        memory_gb = self._memory_gb(memory)
        return ComputeNode(
            id=node_id,
            tenant_id=tenant_id,
            site_id=site_id,
            topology_asset_id=str(labels.get("computeweaver.io/topology-asset", node_id)),
            gpus=gpus,
            cpu_cores=cpu_cores,
            memory_gb=memory_gb,
            state=NodeState.READY if ready else NodeState.FAILED,
            labels={str(key): str(value) for key, value in labels.items()},
        )

    @staticmethod
    def _memory_gb(value: str) -> Decimal:
        units = {"Ki": Decimal(1024), "Mi": Decimal(1024**2), "Gi": Decimal(1024**3)}
        for suffix, factor in units.items():
            if value.endswith(suffix):
                return Decimal(value[: -len(suffix)]) * factor / Decimal(1_000_000_000)
        return Decimal(value or "0") / Decimal(1_000_000_000)

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
                response = client.get("/version")
                return response.status_code == 200
        except (httpx.HTTPError, OSError, PermissionError, ValueError):
            return False

    def dry_run(self, action: dict[str, object]) -> dict[str, object]:
        return {"adapter": self.name, "read_only": True, "would_apply": dict(action)}
