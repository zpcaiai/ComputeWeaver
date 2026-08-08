from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Constraint:
    id: str
    severity: str
    owner: str
    enforcement: str
    description: str


class ConstraintCatalog:
    def __init__(self) -> None:
        self._items: dict[str, Constraint] = {}

    def register(self, constraint: Constraint) -> None:
        if constraint.id in self._items and self._items[constraint.id] != constraint:
            raise ValueError("constraint ID cannot be redefined")
        self._items[constraint.id] = constraint

    def require(self, constraint_id: str) -> Constraint:
        try:
            return self._items[constraint_id]
        except KeyError as error:
            raise ValueError(f"unknown governed constraint {constraint_id}") from error

    def all(self) -> tuple[Constraint, ...]:
        return tuple(sorted(self._items.values(), key=lambda item: item.id))
