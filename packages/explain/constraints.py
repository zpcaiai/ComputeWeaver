from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ConstraintSlack:
    constraint_id: str
    limit: Decimal
    actual: Decimal
    slack: Decimal
    binding: bool
    near_binding: bool


def explain_constraint(
    constraint_id: str,
    *,
    actual: Decimal,
    limit: Decimal,
    tolerance: Decimal = Decimal("0.05"),
) -> ConstraintSlack:
    if limit < 0 or tolerance < 0:
        raise ValueError("constraint limit and tolerance cannot be negative")
    slack = limit - actual
    binding = slack <= 0
    denominator = max(abs(limit), Decimal(1))
    near = not binding and slack / denominator <= tolerance
    return ConstraintSlack(constraint_id, limit, actual, slack, binding, near)


def binding_constraints(items: tuple[ConstraintSlack, ...]) -> tuple[str, ...]:
    return tuple(item.constraint_id for item in items if item.binding or item.near_binding)
