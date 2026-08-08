from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol


class EnergyGateway(Protocol):
    def dry_run(self, target: str, kind: str, parameters: dict[str, Any]) -> dict[str, Any]: ...

    def execute(
        self,
        target: str,
        kind: str,
        parameters: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]: ...


PROHIBITED_FIELDS = frozenset({"breaker", "relay", "firmware", "contactor", "register_address"})


@dataclass(frozen=True, slots=True)
class EnergyPlanAdapter:
    gateway: EnergyGateway
    maximum_dispatch_kw: Decimal

    def validate(self, parameters: dict[str, Any]) -> None:
        if PROHIBITED_FIELDS.intersection(parameters):
            raise PermissionError("low-level electrical control is prohibited")
        dispatch = Decimal(str(parameters.get("dispatch_kw", 0)))
        if abs(dispatch) > self.maximum_dispatch_kw:
            raise ValueError("energy dispatch exceeds certified bound")
        if "valid_until" not in parameters:
            raise ValueError("energy plan requires an expiry")

    def dry_run(self, target: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.validate(parameters)
        return self.gateway.dry_run(target, "set_dispatch_plan", parameters)

    def execute(
        self,
        target: str,
        parameters: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self.validate(parameters)
        if len(idempotency_key) < 8:
            raise ValueError("energy action idempotency key is too short")
        return self.gateway.execute(
            target,
            "set_dispatch_plan",
            parameters,
            idempotency_key=idempotency_key,
        )
