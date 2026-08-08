from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SimulatorExecutor:
    state: dict[str, Any] = field(default_factory=dict)

    def dry_run(self, kind: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"valid": True, "kind": kind, "would_apply": dict(parameters)}

    def execute(self, kind: str, parameters: dict[str, Any]) -> dict[str, Any]:
        previous = dict(self.state)
        self.state[kind] = dict(parameters)
        return {"status": "executed", "previous": previous, "current": dict(self.state)}

    def compensate(self, evidence: dict[str, Any]) -> dict[str, Any]:
        self.state = dict(evidence["previous"])
        return {"status": "compensated", "current": dict(self.state)}


class ExternalReadOnlyExecutor:
    def dry_run(self, kind: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"valid": True, "read_only": True, "kind": kind, "would_apply": parameters}

    def execute(self, kind: str, parameters: dict[str, Any]) -> None:
        del kind, parameters
        raise PermissionError("external execution remains read-only until guarded certification")
