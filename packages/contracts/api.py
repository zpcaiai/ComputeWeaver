from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ErrorDetail(BaseModel):
    field: str | None = None
    message: str


class ErrorEnvelope(BaseModel):
    code: str
    message: str
    correlation_id: str
    details: list[ErrorDetail] = Field(default_factory=list)
    retryable: bool = False


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    total: int


@dataclass(frozen=True, slots=True)
class RequestContext:
    tenant_id: str
    site_id: str | None
    correlation_id: str
    actor_id: str
    roles: frozenset[str]

    def require_role(self, *allowed: str) -> None:
        if not self.roles.intersection(allowed):
            raise PermissionError(f"one of roles {allowed!r} is required")


class IdempotencyRecord(BaseModel):
    key: str
    request_hash: str
    response_status: int
    response_body: dict[str, object]


def require_if_match(current_etag: str, supplied: str | None) -> None:
    if supplied is None:
        raise ValueError("If-Match header is required")
    if supplied != current_etag:
        raise RuntimeError("optimistic concurrency conflict")
