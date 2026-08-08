from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from decimal import Decimal
from threading import RLock
from typing import Any, cast

from packages.optimization.engine import OptimizationResult, optimize
from packages.scheduling.contracts import Allocation, ScheduleInput, SchedulePlan

from .state_estimator import ObservedState, StateDiff, reconcile


@dataclass(frozen=True, slots=True)
class MpcCycle:
    id: int
    started_at: datetime
    observed_state: ObservedState
    state_diff: StateDiff
    previous_plan: SchedulePlan | None
    new_plan: SchedulePlan
    current_allocations: tuple[Allocation, ...]
    fallback_status: str | None
    revision_reasons: tuple[str, ...]


class MpcController:
    def __init__(self, stability_penalty: Decimal = Decimal("0.01")) -> None:
        self.stability_penalty = stability_penalty
        self._last_safe: SchedulePlan | None = None
        self._cycles: list[MpcCycle] = []
        self._cycle_sequence = 0
        self._executed_slots: set[int] = set()
        self._lock = RLock()

    def cycle(
        self,
        request: ScheduleInput,
        observed: ObservedState,
        *,
        started_at: datetime,
        planned_progress: dict[str, Decimal] | None = None,
        timeout_seconds: float = 5,
    ) -> MpcCycle:
        with self._lock:
            if any(slot.index in self._executed_slots for slot in request.slots):
                raise ValueError("already executed intervals cannot be rewritten")
            previous = self._last_safe
            diff = reconcile(planned_progress or {}, observed)
            result = optimize(request, timeout_seconds)
            fallback: str | None = None
            reasons: list[str] = []
            if diff.material:
                reasons.append("OBSERVED_STATE_DIVERGENCE")
            if observed.data_quality < Decimal("0.6"):
                result = OptimizationResult(
                    "quality_blocked", None, {}, "none", Decimal(1), 0, (), ("LOW_DATA_QUALITY",)
                )
            if result.plan is None:
                if previous is None:
                    raise RuntimeError("no feasible plan and no tested fallback")
                plan = previous
                fallback = "last_safe_plan"
                reasons.extend(result.diagnostics)
            else:
                plan = result.plan
                if previous and self._churn(previous, plan) > Decimal("0.5") and not diff.material:
                    plan = previous
                    fallback = "stability_hold"
                    reasons.append("PLAN_CHURN_LIMIT")
                else:
                    self._last_safe = plan
                    reasons.append("OPTIMIZATION_RESULT")
            current_index = min(
                (slot.index for slot in request.slots if slot.index not in self._executed_slots),
                default=None,
            )
            current = tuple(
                replace(allocation, slot_indices=(current_index,))
                for allocation in plan.allocations
                if current_index is not None and current_index in allocation.slot_indices
            )
            if current_index is not None:
                self._executed_slots.add(current_index)
            self._cycle_sequence += 1
            cycle = MpcCycle(
                self._cycle_sequence,
                started_at,
                observed,
                diff,
                previous,
                plan,
                current,
                fallback,
                tuple(reasons),
            )
            self._cycles.append(cycle)
            return cycle

    @staticmethod
    def _churn(left: SchedulePlan, right: SchedulePlan) -> Decimal:
        left_set = {(item.job_id, item.slot_indices) for item in left.allocations}
        right_set = {(item.job_id, item.slot_indices) for item in right.allocations}
        total = max(1, len(left_set | right_set))
        return Decimal(len(left_set ^ right_set)) / Decimal(total)

    @property
    def cycles(self) -> tuple[MpcCycle, ...]:
        return tuple(self._cycles)

    def export_state(self) -> dict[str, object]:
        return {
            "stability_penalty": str(self.stability_penalty),
            "last_safe": asdict(self._last_safe) if self._last_safe else None,
            "executed_slots": sorted(self._executed_slots),
            "cycle_sequence": self._cycle_sequence,
        }

    @classmethod
    def from_state(cls, document: dict[str, object]) -> MpcController:
        controller = cls(Decimal(str(document.get("stability_penalty", "0.01"))))
        raw_plan = document.get("last_safe")
        if isinstance(raw_plan, dict):
            plan = cast(dict[str, Any], raw_plan)
            controller._last_safe = SchedulePlan(
                strategy=str(plan["strategy"]),
                allocations=tuple(
                    Allocation(
                        str(item["job_id"]),
                        tuple(int(index) for index in item["slot_indices"]),
                        int(item["gpu_count"]),
                        str(item["reason_code"]),
                    )
                    for item in cast(list[dict[str, Any]], plan["allocations"])
                ),
                unscheduled=tuple(str(item) for item in cast(list[Any], plan["unscheduled"])),
                energy_intent_kwh=Decimal(str(plan["energy_intent_kwh"])),
                estimated_cost=Decimal(str(plan["estimated_cost"])),
                assumptions=tuple(str(item) for item in cast(list[Any], plan["assumptions"])),
                violations=tuple(str(item) for item in cast(list[Any], plan["violations"])),
                input_hash=str(plan["input_hash"]),
            )
        controller._executed_slots = {int(item) for item in cast(list[Any], document.get("executed_slots", []))}
        controller._cycle_sequence = int(str(document.get("cycle_sequence", 0)))
        return controller
