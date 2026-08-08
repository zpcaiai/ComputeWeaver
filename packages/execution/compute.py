from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ComputeGateway(Protocol):
    def dry_run(self, target: str, kind: str, parameters: dict[str, Any]) -> dict[str, Any]: ...

    def execute(
        self,
        target: str,
        kind: str,
        parameters: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ComputeActionAdapter:
    gateway: ComputeGateway
    allowed_kinds: frozenset[str] = frozenset({"schedule_job", "cancel_job", "pause_checkpointable_job", "resume_job"})

    def dry_run(self, target: str, kind: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self._validate(kind, parameters)
        return self.gateway.dry_run(target, kind, parameters)

    def execute(
        self,
        target: str,
        kind: str,
        parameters: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._validate(kind, parameters)
        if len(idempotency_key) < 8:
            raise ValueError("compute action idempotency key is too short")
        return self.gateway.execute(target, kind, parameters, idempotency_key=idempotency_key)

    def _validate(self, kind: str, parameters: dict[str, Any]) -> None:
        if kind not in self.allowed_kinds:
            raise PermissionError("compute action is not allowlisted")
        if not parameters:
            raise ValueError("compute action parameters are required")
