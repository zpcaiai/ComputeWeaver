from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass

from packages.compute.inventory import ComputeNode
from packages.compute.snapshot import ComputeSnapshot, SnapshotBuilder


class ComputeAdapter(ABC):
    read_only: bool = True

    @abstractmethod
    def discover(self) -> tuple[ComputeNode, ...]: ...

    @abstractmethod
    def snapshot(self, tenant_id: str, topology_version: int) -> ComputeSnapshot: ...

    @abstractmethod
    def watch(self) -> Iterator[ComputeSnapshot]: ...

    @abstractmethod
    def validate_credentials(self) -> bool: ...

    @abstractmethod
    def dry_run(self, action: dict[str, object]) -> dict[str, object]: ...


class ReadOnlyAdapter:
    read_only = True

    def execute(self, action: dict[str, object]) -> None:
        del action
        raise PermissionError("external adapter is read-only; use guarded execution")


@dataclass(slots=True)
class StaticComputeAdapter(ComputeAdapter, ReadOnlyAdapter):
    name: str
    nodes: tuple[ComputeNode, ...]

    def discover(self) -> tuple[ComputeNode, ...]:
        return self.nodes

    def snapshot(self, tenant_id: str, topology_version: int) -> ComputeSnapshot:
        return SnapshotBuilder().build(
            tenant_id=tenant_id,
            topology_version=topology_version,
            source=self.name,
            nodes=self.nodes,
        )

    def watch(self) -> Iterator[ComputeSnapshot]:
        tenant_id = self.nodes[0].tenant_id if self.nodes else "unknown"
        yield self.snapshot(tenant_id, 1)

    def validate_credentials(self) -> bool:
        return True

    def dry_run(self, action: dict[str, object]) -> dict[str, object]:
        return {"adapter": self.name, "read_only": True, "would_apply": dict(action)}
