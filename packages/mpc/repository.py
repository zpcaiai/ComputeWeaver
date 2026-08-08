from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from apps.api.store import Store
from packages.scheduling.contracts import ScheduleInput

from .controller import MpcController, MpcCycle
from .state_estimator import ObservedState


class MpcRepository:
    def __init__(self, store: Store) -> None:
        self.store = store

    def create(
        self,
        controller_id: str,
        tenant_id: str,
        *,
        stability_penalty: Decimal = Decimal("0.01"),
        idempotency_key: str,
    ) -> dict[str, Any]:
        controller = MpcController(stability_penalty)
        resource = self.store.put(
            kind="mpc_controller_state",
            resource_id=controller_id,
            tenant_id=tenant_id,
            body=controller.export_state(),
            idempotency_key=f"mpc-create:{idempotency_key}",
        )
        return {"id": controller_id, "version": resource.version, "etag": resource.etag}

    def cycle(
        self,
        controller_id: str,
        tenant_id: str,
        request: ScheduleInput,
        observed: ObservedState,
        *,
        started_at: datetime,
        timeout_seconds: float,
        idempotency_key: str,
    ) -> MpcCycle:
        try:
            resource = self.store.get("mpc_controller_state", controller_id, tenant_id)
        except KeyError:
            self.create(
                controller_id,
                tenant_id,
                idempotency_key=f"implicit-{controller_id}",
            )
            resource = self.store.get("mpc_controller_state", controller_id, tenant_id)
        controller = MpcController.from_state(resource.body)
        cycle = controller.cycle(
            request,
            observed,
            started_at=started_at,
            timeout_seconds=timeout_seconds,
        )
        self.store.put(
            kind="mpc_controller_state",
            resource_id=controller_id,
            tenant_id=tenant_id,
            body=controller.export_state(),
            idempotency_key=f"mpc-state:{idempotency_key}",
            if_match=resource.etag,
        )
        self.store.put(
            kind="mpc_cycle",
            resource_id=f"{controller_id}-{cycle.id}",
            tenant_id=tenant_id,
            body={**asdict(cycle), "cycle_id": cycle.id},
            idempotency_key=f"mpc-cycle:{idempotency_key}",
        )
        return cycle
