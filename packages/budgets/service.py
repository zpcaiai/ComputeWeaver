from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from threading import RLock


@dataclass(frozen=True, slots=True)
class Budget:
    tenant_id: str
    cost_limit: Decimal
    carbon_limit_kg: Decimal
    power_limit_kw: Decimal
    quota_gpu_hours: Decimal
    warn_at: Decimal = Decimal("0.8")


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    cost_used: Decimal
    carbon_used: Decimal
    gpu_hours_used: Decimal
    warning: bool
    exceeded: bool


class BudgetLedger:
    def __init__(self) -> None:
        self._budgets: dict[str, Budget] = {}
        self._usage: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
        self._lock = RLock()

    def configure(self, budget: Budget) -> None:
        self._budgets[budget.tenant_id] = budget

    def record(self, tenant_id: str, *, cost: Decimal, carbon_kg: Decimal, gpu_hours: Decimal) -> BudgetStatus:
        if min(cost, carbon_kg, gpu_hours) < 0:
            raise ValueError("budget usage cannot be negative")
        with self._lock:
            current = self._usage.get(tenant_id, (Decimal(0), Decimal(0), Decimal(0)))
            updated = (current[0] + cost, current[1] + carbon_kg, current[2] + gpu_hours)
            self._usage[tenant_id] = updated
            return self.status(tenant_id)

    def status(self, tenant_id: str) -> BudgetStatus:
        budget = self._budgets[tenant_id]
        cost, carbon, gpu_hours = self._usage.get(tenant_id, (Decimal(0), Decimal(0), Decimal(0)))
        ratios = (
            cost / budget.cost_limit if budget.cost_limit else Decimal(1),
            carbon / budget.carbon_limit_kg if budget.carbon_limit_kg else Decimal(1),
            gpu_hours / budget.quota_gpu_hours if budget.quota_gpu_hours else Decimal(1),
        )
        return BudgetStatus(cost, carbon, gpu_hours, max(ratios) >= budget.warn_at, max(ratios) >= 1)
