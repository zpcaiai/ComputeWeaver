from __future__ import annotations

import copy
import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    seed: int = 7
    duration_hours: int = 24
    step_minutes: int = 15
    gpu_count: int = 16
    pv_capacity_kw: Decimal = Decimal(100)
    battery_capacity_kwh: Decimal = Decimal(200)
    environment: str = "simulator"
    real_endpoints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.environment != "simulator":
            raise ValueError("simulator requires environment=simulator")
        if self.real_endpoints:
            raise PermissionError("real endpoints are prohibited in simulator")
        if self.step_minutes <= 0 or self.duration_hours <= 0:
            raise ValueError("simulation time settings must be positive")


@dataclass(slots=True)
class SimulationState:
    step: int
    timestamp: datetime
    battery_soc: Decimal
    active_jobs: int
    events: list[dict[str, Any]]


class Simulator:
    def __init__(self, config: SimulationConfig) -> None:
        self.config = config
        self.random = random.Random(config.seed)
        self.state = SimulationState(
            step=0,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            battery_soc=Decimal("0.5"),
            active_jobs=0,
            events=[],
        )
        self.running = False

    @property
    def total_steps(self) -> int:
        return self.config.duration_hours * 60 // self.config.step_minutes

    def start(self) -> None:
        self.running = True

    def pause(self) -> None:
        self.running = False

    def step(self, fault: str | None = None) -> dict[str, Any]:
        if self.state.step >= self.total_steps:
            raise StopIteration
        hour = self.state.timestamp.hour + self.state.timestamp.minute / 60
        pv = max(0.0, math.sin(math.pi * (hour - 6) / 12)) * float(self.config.pv_capacity_kw)
        forecast_pv = pv
        if fault == "pv_surplus":
            pv = max(pv, float(self.config.pv_capacity_kw) * 0.9)
        elif fault == "pv_error":
            pv *= 0.4
        arrivals = 1 if self.random.random() < 0.12 else 0
        if fault == "job_burst":
            arrivals += 8
        elif fault == "urgent_job":
            arrivals += 1
        completions = 1 if self.state.active_jobs and self.random.random() < 0.08 else 0
        self.state.active_jobs = max(0, self.state.active_jobs + arrivals - completions)
        compute_kw = min(self.config.gpu_count, self.state.active_jobs * 2) * 0.6
        if fault == "gpu_failure":
            compute_kw *= 0.5
        facility_kw = compute_kw * 1.25 + 8
        net_kw = facility_kw - pv
        charge = min(max(-net_kw, 0), 30.0)
        discharge = min(max(net_kw, 0), 30.0) if self.state.battery_soc > Decimal("0.2") else 0.0
        if fault == "battery_unavailable":
            charge = discharge = 0.0
        delta = (charge * 0.95 - discharge / 0.95) * self.config.step_minutes / 60
        soc = float(self.state.battery_soc) + delta / float(self.config.battery_capacity_kwh)
        self.state.battery_soc = Decimal(str(min(0.9, max(0.1, soc))))
        grid_kw = max(0.0, net_kw + charge - discharge)
        grid_limit_kw = float("inf")
        if fault in {"grid_derating", "island_mode"}:
            grid_limit_kw = 20.0 if fault == "grid_derating" else 0.0
            grid_kw = min(grid_kw, grid_limit_kw)
        supply_kw = grid_kw + pv + discharge
        demand_kw = facility_kw + charge
        unserved_kw = max(0.0, demand_kw - supply_kw)
        price_per_kwh = 0.8 if fault == "high_price" else 0.1
        event = {
            "sequence": self.state.step,
            "timestamp": self.state.timestamp.isoformat(),
            "active_jobs": self.state.active_jobs,
            "compute_kw": round(compute_kw, 6),
            "facility_kw": round(facility_kw, 6),
            "pv_kw": round(pv, 6),
            "forecast_pv_kw": round(forecast_pv, 6),
            "grid_kw": round(grid_kw, 6),
            "grid_limit_kw": None if math.isinf(grid_limit_kw) else grid_limit_kw,
            "unserved_kw": round(unserved_kw, 6),
            "price_per_kwh": price_per_kwh,
            "battery_soc": str(self.state.battery_soc),
            "fault": fault,
            "urgent_job": fault == "urgent_job",
        }
        self.state.events.append(event)
        self.state.step += 1
        self.state.timestamp += timedelta(minutes=self.config.step_minutes)
        return event

    def run(self, fault_schedule: dict[int, str] | None = None) -> list[dict[str, Any]]:
        self.start()
        faults = fault_schedule or {}
        while self.state.step < self.total_steps:
            self.step(faults.get(self.state.step))
        self.pause()
        return copy.deepcopy(self.state.events)

    def snapshot(self) -> dict[str, Any]:
        return {
            "config": asdict(self.config),
            "state": copy.deepcopy(self.state),
            "random_state": self.random.getstate(),
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        if snapshot["config"] != asdict(self.config):
            raise ValueError("snapshot configuration mismatch")
        self.state = copy.deepcopy(snapshot["state"])
        self.random.setstate(snapshot["random_state"])

    def event_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.state.events, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
