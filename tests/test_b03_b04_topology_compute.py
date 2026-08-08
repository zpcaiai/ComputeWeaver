from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from packages.compute.inventory import ComputeNode, Gpu, MigSlice, Reservation
from packages.compute.snapshot import SnapshotBuilder
from packages.connectors.kubernetes import KubernetesAdapter
from packages.topology.models import Asset, AssetType, Relationship
from packages.topology.registry import TopologyRegistry
from packages.topology.validation import validate_graph


def assets() -> tuple[Asset, ...]:
    return (
        Asset("site-one", "tenant-one", "site-one", AssetType.SITE, "Site", Decimal(100)),
        Asset("rack-one", "tenant-one", "site-one", AssetType.RACK, "Rack", Decimal(50)),
        Asset("node-one", "tenant-one", "site-one", AssetType.COMPUTE_NODE, "Node", Decimal(10)),
    )


def test_topology_rejects_cycle_orphan_and_capacity() -> None:
    topology_assets = assets()
    with pytest.raises(ValueError, match="cycle"):
        validate_graph(
            topology_assets,
            (
                Relationship("site-one", "rack-one"),
                Relationship("rack-one", "node-one"),
                Relationship("node-one", "site-one"),
            ),
        )
    with pytest.raises(ValueError, match="orphan"):
        validate_graph(topology_assets, (Relationship("missing", "rack-one"),))
    too_large = topology_assets + (Asset("rack-two", "tenant-one", "site-one", AssetType.RACK, "Rack 2", Decimal(60)),)
    with pytest.raises(ValueError, match="capacity"):
        validate_graph(
            too_large,
            (Relationship("site-one", "rack-one"), Relationship("site-one", "rack-two")),
        )


def test_published_topology_is_versioned_and_traversable() -> None:
    registry = TopologyRegistry()
    draft = registry.create_draft(
        "tenant-one",
        assets(),
        (Relationship("site-one", "rack-one"), Relationship("rack-one", "node-one")),
    )
    assert draft.endswith("-1")
    first = registry.publish("tenant-one", expected_draft_revision=1)
    assert first.version == 1
    assert {item.id for item in registry.traverse("tenant-one", "site-one")} == {
        "rack-one",
        "node-one",
    }
    with pytest.raises(RuntimeError):
        registry.publish("tenant-one", expected_draft_revision=2)


def test_mig_accounting_and_duplicate_gpu_protection(now: datetime) -> None:
    with pytest.raises(ValueError, match="over-allocate"):
        Gpu(
            "gpu-one",
            "H100",
            Decimal(80),
            Decimal("0.7"),
            (
                MigSlice("mig-a", Decimal(40), Decimal("0.6")),
                MigSlice("mig-b", Decimal(40), Decimal("0.6")),
            ),
        )
    gpu = Gpu("gpu-one", "H100", Decimal(80), Decimal("0.7"))
    nodes = (
        ComputeNode(
            "node-one",
            "tenant-one",
            "site-one",
            "node-one",
            (gpu,),
            32,
            Decimal(256),
        ),
        ComputeNode(
            "node-two",
            "tenant-one",
            "site-one",
            "node-two",
            (gpu,),
            32,
            Decimal(256),
        ),
    )
    with pytest.raises(ValueError, match="duplicate GPU"):
        SnapshotBuilder().build(tenant_id="tenant-one", topology_version=1, source="test", nodes=nodes, now=now)


def test_snapshot_freshness_conflicts_reservation_and_adapter_read_only(now: datetime) -> None:
    gpu = Gpu("gpu-one", "H100", Decimal(80), Decimal("0.7"))
    node = ComputeNode("node-one", "tenant-one", "site-one", "node-one", (gpu,), 32, Decimal(256))
    reservation = Reservation(
        "reserve-one",
        "tenant-one",
        frozenset({"gpu-one"}),
        now,
        now + timedelta(hours=1),
        "online-inference",
        True,
    )
    snapshot = SnapshotBuilder().build(
        tenant_id="tenant-one",
        topology_version=1,
        source="kubernetes",
        nodes=(node,),
        reservations=(reservation,),
        observed_at=now - timedelta(minutes=10),
        now=now,
        scheduler_states={"node-one": "ready"},
        telemetry_states={"node-one": "failed"},
    )
    assert snapshot.schedulable_gpu_count == 0
    assert snapshot.quality == "conflict"
    adapter = KubernetesAdapter("kubernetes", (node,))
    assert adapter.validate_credentials()
    assert adapter.dry_run({"cordon": "node-one"})["read_only"] is True
    with pytest.raises(PermissionError):
        adapter.execute({"cordon": "node-one"})
