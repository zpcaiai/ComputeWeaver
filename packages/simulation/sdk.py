from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .engine import Simulator


@dataclass(slots=True)
class SimulationSdk:
    simulator: Simulator

    def command(self, name: str, **parameters: Any) -> Any:
        if name == "step":
            return self.simulator.step(parameters.get("fault"))
        if name == "snapshot":
            return self.simulator.snapshot()
        if name == "restore":
            return self.simulator.restore(parameters["snapshot"])
        if name == "run":
            return self.simulator.run(parameters.get("fault_schedule"))
        raise ValueError(f"unsupported simulator command {name}")

    def observe(self) -> dict[str, Any]:
        state = self.simulator.state
        return {
            "step": state.step,
            "timestamp": state.timestamp.isoformat(),
            "battery_soc": str(state.battery_soc),
            "active_jobs": state.active_jobs,
        }
