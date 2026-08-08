from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from apps.api.store import Store, StoredResource

from .engine import SimulationConfig, SimulationState, Simulator


def _tuple_tree(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_tuple_tree(item) for item in value)
    return value


def serialize_simulator(simulator: Simulator) -> dict[str, Any]:
    config = simulator.config
    state = simulator.state
    return {
        "config": {
            "seed": config.seed,
            "duration_hours": config.duration_hours,
            "step_minutes": config.step_minutes,
            "gpu_count": config.gpu_count,
            "pv_capacity_kw": str(config.pv_capacity_kw),
            "battery_capacity_kwh": str(config.battery_capacity_kwh),
            "environment": config.environment,
            "real_endpoints": list(config.real_endpoints),
        },
        "state": {
            "step": state.step,
            "timestamp": state.timestamp.isoformat(),
            "battery_soc": str(state.battery_soc),
            "active_jobs": state.active_jobs,
            "events": state.events,
        },
        "random_state": simulator.random.getstate(),
        "running": simulator.running,
        "event_hash": simulator.event_hash(),
    }


def restore_simulator(document: dict[str, Any]) -> Simulator:
    config_data = dict(document["config"])
    simulator = Simulator(
        SimulationConfig(
            seed=int(config_data["seed"]),
            duration_hours=int(config_data["duration_hours"]),
            step_minutes=int(config_data["step_minutes"]),
            gpu_count=int(config_data["gpu_count"]),
            pv_capacity_kw=Decimal(str(config_data["pv_capacity_kw"])),
            battery_capacity_kwh=Decimal(str(config_data["battery_capacity_kwh"])),
            environment=str(config_data["environment"]),
            real_endpoints=tuple(str(item) for item in config_data.get("real_endpoints", [])),
        )
    )
    state_data = dict(document["state"])
    simulator.state = SimulationState(
        int(state_data["step"]),
        datetime.fromisoformat(str(state_data["timestamp"])),
        Decimal(str(state_data["battery_soc"])),
        int(state_data["active_jobs"]),
        [dict(item) for item in state_data["events"]],
    )
    simulator.random.setstate(cast(tuple[Any, ...], _tuple_tree(document["random_state"])))
    simulator.running = bool(document.get("running", False))
    if simulator.event_hash() != document["event_hash"]:
        raise ValueError("persisted simulation event hash does not match state")
    return simulator


class SimulationRepository:
    def __init__(self, store: Store) -> None:
        self.store = store

    def create(
        self,
        simulation_id: str,
        tenant_id: str,
        config: SimulationConfig,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        resource = self.store.put(
            kind="simulation_session",
            resource_id=simulation_id,
            tenant_id=tenant_id,
            body=serialize_simulator(Simulator(config)),
            idempotency_key=f"simulation-create:{idempotency_key}",
        )
        return {
            "id": simulation_id,
            "status": "created",
            "environment": config.environment,
            "version": resource.version,
            "etag": resource.etag,
        }

    def operate(
        self,
        simulation_id: str,
        tenant_id: str,
        operation: str,
        parameters: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        resource = self.store.get("simulation_session", simulation_id, tenant_id)
        simulator = restore_simulator(resource.body)
        if operation == "start":
            simulator.start()
            result: dict[str, Any] = {"status": "running"}
        elif operation == "pause":
            simulator.pause()
            result = {"status": "paused"}
        elif operation == "step":
            result = simulator.step(parameters.get("fault"))
        elif operation == "snapshot":
            return self._snapshot(simulation_id, tenant_id, resource, idempotency_key=idempotency_key)
        elif operation == "restore":
            return self._restore(
                simulation_id,
                tenant_id,
                resource,
                str(parameters["snapshot_token"]),
                idempotency_key=idempotency_key,
            )
        else:
            raise ValueError(f"unsupported simulation operation {operation}")
        updated = self._save(resource, simulator, idempotency_key)
        return {**result, "version": updated.version, "etag": updated.etag}

    def _snapshot(
        self,
        simulation_id: str,
        tenant_id: str,
        resource: StoredResource,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        canonical = json.dumps(resource.body, default=str, separators=(",", ":"), sort_keys=True).encode()
        token = hashlib.sha256(canonical).hexdigest()
        self.store.put(
            kind="simulation_snapshot",
            resource_id=token,
            tenant_id=tenant_id,
            body={"simulation_id": simulation_id, "session": resource.body},
            idempotency_key=f"simulation-snapshot:{idempotency_key}",
        )
        state = dict(resource.body["state"])
        return {
            "snapshot_token": token,
            "state": state,
            "event_hash": resource.body["event_hash"],
            "version": resource.version,
            "etag": resource.etag,
        }

    def _restore(
        self,
        simulation_id: str,
        tenant_id: str,
        current: StoredResource,
        token: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        snapshot = self.store.get("simulation_snapshot", token, tenant_id)
        if snapshot.body["simulation_id"] != simulation_id:
            raise ValueError("snapshot belongs to a different simulation")
        simulator = restore_simulator(dict(snapshot.body["session"]))
        updated = self._save(current, simulator, idempotency_key)
        return {
            "status": "restored",
            "event_hash": simulator.event_hash(),
            "version": updated.version,
            "etag": updated.etag,
        }

    def _save(self, resource: StoredResource, simulator: Simulator, idempotency_key: str) -> StoredResource:
        return self.store.put(
            kind="simulation_session",
            resource_id=resource.id,
            tenant_id=resource.tenant_id,
            body=serialize_simulator(simulator),
            idempotency_key=f"simulation-operation:{idempotency_key}",
            if_match=resource.etag,
        )
