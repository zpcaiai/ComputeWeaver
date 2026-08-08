from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal


@dataclass(frozen=True, slots=True)
class Allocation:
    dimension: str
    value: str
    weight: Decimal
    amount: Decimal


def allocate_cost(total: Decimal, weights: dict[tuple[str, str], Decimal], places: int = 2) -> tuple[Allocation, ...]:
    if total < 0 or any(weight < 0 for weight in weights.values()):
        raise ValueError("cost and weights cannot be negative")
    denominator = sum(weights.values(), Decimal(0))
    if denominator <= 0:
        raise ValueError("positive allocation weight is required")
    quantum = Decimal(1).scaleb(-places)
    ordered = sorted(weights.items())
    allocations: list[Allocation] = []
    assigned = Decimal(0)
    for index, ((dimension, value), weight) in enumerate(ordered):
        amount = (
            total - assigned
            if index == len(ordered) - 1
            else (total * weight / denominator).quantize(quantum, rounding=ROUND_HALF_EVEN)
        )
        assigned += amount
        allocations.append(Allocation(dimension, value, weight, amount))
    if sum((item.amount for item in allocations), Decimal(0)) != total:
        raise RuntimeError("chargeback reconciliation failed")
    return tuple(allocations)
