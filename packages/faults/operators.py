from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FaultOperator:
    kind: str
    target: str
    magnitude: float = 1.0

    def apply(self, state: dict[str, Any]) -> dict[str, Any]:
        output = dict(state)
        existing = state.get("faults", [])
        if not isinstance(existing, list):
            raise ValueError("fault state must contain a list of faults")
        output["faults"] = [
            *existing,
            {"kind": self.kind, "target": self.target, "magnitude": self.magnitude},
        ]
        return output
