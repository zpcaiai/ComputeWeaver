from __future__ import annotations

import tomllib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from config.settings import Settings
from packages.contracts.events import EventEnvelope, EventStore
from packages.contracts.schema_cli import compatible
from packages.domain.identity import validate_id
from packages.domain.time import TimeInterval
from packages.domain.units import Duration, Energy, Money, Percentage, Power
from packages.observability.audit import AuditLog


def test_production_wheel_includes_runtime_configuration_package() -> None:
    project = Path(__file__).resolve().parents[1]
    configuration = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    wheel_packages = configuration["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert {"apps", "packages", "config", "scripts"}.issubset(wheel_packages)
    dockerfile = (project / "deploy" / "compose" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY config config" in dockerfile
    assert "COPY scripts scripts" in dockerfile


@given(st.decimals(min_value=0, max_value=10000, allow_nan=False, allow_infinity=False))
def test_power_energy_round_trip(value: Decimal) -> None:
    duration = Duration("0.25")
    power = Power(value)
    assert power.energy_for(duration).average_power(duration).kw == value


def test_incompatible_units_and_currency_are_rejected() -> None:
    with pytest.raises(TypeError):
        Power(10).energy_for(Energy(10))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="currency mismatch"):
        _ = Money(1, "USD") + Money(1, "CNY")
    with pytest.raises(ValueError):
        Percentage("1.01")


def test_intervals_are_half_open_and_timezone_aware() -> None:
    start = datetime(2026, 3, 8, 1, tzinfo=UTC)
    interval = TimeInterval(start, start + timedelta(hours=2))
    assert interval.contains(start)
    assert not interval.contains(interval.end)
    assert len(interval.split(timedelta(minutes=30))) == 4
    with pytest.raises(ValueError):
        TimeInterval(datetime(2026, 1, 1), datetime(2026, 1, 2))


def test_event_round_trip_append_only_and_scope() -> None:
    event = EventEnvelope(
        event_type="JobSubmitted",
        tenant_id="tenant-one",
        site_id="site-one",
        trace_id="trace-1",
        payload={"job_id": "job-one"},
    )
    restored = EventEnvelope.model_validate_json(event.model_dump_json())
    assert restored == event
    store = EventStore()
    assert store.append(event)
    assert not store.append(event)
    assert store.query("tenant-one") == (event,)
    assert store.query("tenant-two") == ()


def test_schema_breaking_change_is_detected() -> None:
    old = {"properties": {"id": {}, "value": {}}, "required": ["id"]}
    new = {"properties": {"id": {}, "extra": {}}, "required": ["id", "extra"]}
    ok, issues = compatible(old, new)
    assert not ok
    assert "removed property: value" in issues
    assert "new required property: extra" in issues


def test_external_writes_are_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPUTEWEAVER_EXTERNAL_WRITE_ENABLED", "true")
    monkeypatch.setenv("COMPUTEWEAVER_EXECUTION_MODE", "guarded")
    monkeypatch.setenv("COMPUTEWEAVER_ENV", "development")
    monkeypatch.setenv("COMPUTEWEAVER_RELEASE_CERTIFICATE", "candidate")
    assert not Settings.from_env().external_writes_allowed()


def test_audit_chain_detects_no_mutation() -> None:
    log = AuditLog()
    log.append(
        actor_id="user-one",
        tenant_id="tenant-one",
        action="resource.write",
        resource="resource-one",
        outcome="success",
        correlation_id="correlation-one",
    )
    assert log.verify()


@pytest.mark.parametrize("invalid", ["UPPER", "a", "has space", "../escape"])
def test_identifier_validation(invalid: str) -> None:
    with pytest.raises(ValueError):
        validate_id(invalid)
