from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class ConnectorState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OPEN = "open"


class ConnectorOperation(Protocol):
    def __call__(self) -> Any: ...


@dataclass(slots=True)
class CircuitBreaker:
    failure_threshold: int = 3
    recovery_seconds: float = 30
    failures: int = 0
    opened_at: float | None = None

    @property
    def state(self) -> ConnectorState:
        if self.opened_at is None:
            return ConnectorState.HEALTHY if self.failures == 0 else ConnectorState.DEGRADED
        if time.monotonic() - self.opened_at >= self.recovery_seconds:
            return ConnectorState.DEGRADED
        return ConnectorState.OPEN

    def execute(self, operation: ConnectorOperation, retries: int = 2) -> Any:
        if self.state == ConnectorState.OPEN:
            raise ConnectionError("connector circuit is open")
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                result = operation()
                self.failures = 0
                self.opened_at = None
                return result
            except Exception as error:  # connector boundary intentionally catches provider errors
                last_error = error
                self.failures += 1
                if self.failures >= self.failure_threshold:
                    self.opened_at = time.monotonic()
                    break
                if attempt < retries:
                    time.sleep(min(0.01 * (2**attempt), 0.05))
        raise ConnectionError("connector operation failed") from last_error


@dataclass(frozen=True, slots=True)
class ConnectorStatus:
    id: str
    coverage: frozenset[str]
    last_success: str | None
    lag_seconds: int | None
    quality: str
    permission_scope: frozenset[str]
