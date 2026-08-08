from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from packages.compute.inventory import ComputeNode, Gpu
from packages.compute.snapshot import SnapshotBuilder
from packages.ingestion.raw import RawEvent, RawLanding


@pytest.mark.performance
def test_append_only_landing_handles_bounded_mvp_batch() -> None:
    landing = RawLanding()
    started = time.perf_counter()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(5_000):
        assert landing.append(
            RawEvent.create(
                id=f"performance-{index}",
                tenant_id="tenant-performance",
                source="meter",
                received_at=now + timedelta(seconds=index),
                payload={
                    "metric": "facility_power",
                    "timestamp": (now + timedelta(seconds=index)).isoformat(),
                    "value": str(index),
                    "unit": "kW",
                },
            )
        )
    elapsed = time.perf_counter() - started
    assert len(landing.query("tenant-performance")) == 5_000
    assert elapsed < 5


@pytest.mark.performance
def test_ten_thousand_node_snapshot_serialization_is_bounded() -> None:
    nodes = tuple(
        ComputeNode(
            id=f"node-{index}",
            tenant_id="tenant-performance",
            site_id="site-performance",
            topology_asset_id=f"rack-{index // 40}",
            gpus=(Gpu(f"gpu-{index}", "H100", Decimal(80), Decimal("0.7")),),
            cpu_cores=64,
            memory_gb=Decimal(512),
        )
        for index in range(10_000)
    )
    started = time.perf_counter()
    snapshot = SnapshotBuilder().build(
        tenant_id="tenant-performance",
        topology_version=7,
        source="performance-fixture",
        nodes=nodes,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    encoded = json.dumps(asdict(snapshot), default=str, sort_keys=True).encode()
    elapsed = time.perf_counter() - started

    assert snapshot.schedulable_gpu_count == 10_000
    assert len(encoded) > 1_000_000
    assert elapsed < 8
