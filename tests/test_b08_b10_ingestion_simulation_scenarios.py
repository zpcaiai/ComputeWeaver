from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from packages.connectors.base import CircuitBreaker, ConnectorState
from packages.data_quality.service import assess
from packages.ingestion.normalize import Normalizer
from packages.ingestion.raw import RawEvent, RawLanding
from packages.replay.service import replay
from packages.scenarios.compiler import compile_scenario, run_scenario
from packages.simulation.engine import SimulationConfig, Simulator
from packages.timeseries.store import TimeSeriesStore


def raw(event_id: str, timestamp: datetime, value: str = "1000") -> RawEvent:
    return RawEvent.create(
        id=event_id,
        tenant_id="tenant-one",
        source="meter-one",
        received_at=timestamp,
        payload={
            "metric": "grid_power",
            "timestamp": timestamp.isoformat(),
            "value": value,
            "unit": "W",
        },
    )


def test_raw_lineage_dedup_normalization_and_timeseries(now: datetime) -> None:
    landing = RawLanding()
    event = raw("event-one", now)
    assert landing.append(event)
    assert not landing.append(event)
    point = Normalizer().normalize(event)
    assert point.value == Decimal(1)
    assert point.unit == "kW"
    assert point.raw_payload_hash == event.payload_hash
    store = TimeSeriesStore()
    assert store.append(point)
    assert not store.append(point)
    assert store.query("tenant-one", "grid_power", now, now + timedelta(hours=1)) == (point,)


def test_raw_landing_scopes_reused_event_ids_by_tenant(now: datetime) -> None:
    landing = RawLanding()
    first = raw("shared-event", now)
    second = RawEvent.create(
        id="shared-event",
        tenant_id="tenant-two",
        source="meter-two",
        received_at=now,
        payload={"metric": "grid_power", "value": "2", "unit": "kW"},
    )

    assert landing.append(first)
    assert landing.append(second)
    assert landing.get("shared-event", "tenant-one") == first
    assert landing.get("shared-event", "tenant-two") == second


def test_quality_degrades_and_recovers(now: datetime) -> None:
    points = (Normalizer().normalize(raw("event-one", now, "1000")),)
    degraded = assess(
        points,
        expected_start=now,
        expected_end=now + timedelta(hours=1),
        expected_step=timedelta(minutes=15),
        now=now,
        stale_after=timedelta(hours=1),
        maximum=Decimal(10),
    )
    assert degraded.status != "good"
    complete = tuple(
        Normalizer().normalize(raw(f"event-{index}", now + timedelta(minutes=15 * index), "1000")) for index in range(4)
    )
    recovered = assess(
        complete,
        expected_start=now,
        expected_end=now + timedelta(hours=1),
        expected_step=timedelta(minutes=15),
        now=now + timedelta(minutes=45),
        stale_after=timedelta(hours=1),
    )
    assert recovered.status == "good"


def test_circuit_breaker_opens_and_recovers() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=0)

    def fail() -> None:
        raise OSError("provider unavailable")

    with pytest.raises(ConnectionError):
        breaker.execute(fail, retries=1)
    assert breaker.state == ConnectorState.DEGRADED
    assert breaker.execute(lambda: "ok") == "ok"
    assert breaker.state == ConnectorState.HEALTHY


def test_simulator_determinism_snapshot_restore_and_isolation() -> None:
    left = Simulator(SimulationConfig(seed=11, duration_hours=1))
    right = Simulator(SimulationConfig(seed=11, duration_hours=1))
    left.run()
    right.run()
    assert left.event_hash() == right.event_hash()
    original = Simulator(SimulationConfig(seed=13, duration_hours=1))
    original.step()
    snapshot = original.snapshot()
    future = original.step()
    restored = Simulator(SimulationConfig(seed=13, duration_hours=1))
    restored.restore(snapshot)
    assert restored.step() == future
    with pytest.raises(PermissionError):
        SimulationConfig(real_endpoints=("https://production.example",))


def test_ten_scenario_semantics_and_replay_equivalence() -> None:
    kinds = (
        None,
        "high_price",
        "pv_surplus",
        "job_burst",
        "urgent_job",
        "pv_error",
        "battery_unavailable",
        "gpu_failure",
        "grid_derating",
        "island_mode",
    )
    for index, kind in enumerate(kinds):
        document = {
            "name": f"scenario-{index}",
            "version": "1.0.0",
            "seed": index,
            "duration_hours": 1,
            "faults": [] if kind is None else [{"step": 1, "kind": kind, "target": "site-one"}],
        }
        scenario = compile_scenario(document)
        events, evaluation = run_scenario(scenario)
        assert len(events) == 4
        assert replay(events).evaluation == evaluation


def test_fault_operators_change_the_intended_signal() -> None:
    normal = Simulator(SimulationConfig(seed=3, duration_hours=1)).step()
    high_price = Simulator(SimulationConfig(seed=3, duration_hours=1)).step("high_price")
    surplus = Simulator(SimulationConfig(seed=3, duration_hours=1)).step("pv_surplus")
    burst = Simulator(SimulationConfig(seed=3, duration_hours=1)).step("job_burst")
    pv_error = Simulator(SimulationConfig(seed=3, duration_hours=1)).step("pv_error")
    assert high_price["price_per_kwh"] > normal["price_per_kwh"]
    assert surplus["pv_kw"] > normal["pv_kw"]
    assert burst["active_jobs"] > normal["active_jobs"]
    assert pv_error["pv_kw"] < pv_error["forecast_pv_kw"] or pv_error["forecast_pv_kw"] == 0
