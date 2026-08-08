from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Operation:
    apply: Callable[[], dict[str, Any]]
    compensate: Callable[[dict[str, Any]], dict[str, Any]]


def execute_transaction(operations: tuple[Operation, ...]) -> tuple[dict[str, Any], ...]:
    completed: list[tuple[Operation, dict[str, Any]]] = []
    try:
        for operation in operations:
            evidence = operation.apply()
            completed.append((operation, evidence))
    except Exception:
        compensation_errors: list[Exception] = []
        for operation, evidence in reversed(completed):
            try:
                operation.compensate(evidence)
            except Exception as error:
                compensation_errors.append(error)
        if compensation_errors:
            raise RuntimeError("operation and compensation both failed") from compensation_errors[0]
        raise
    return tuple(evidence for _, evidence in completed)
