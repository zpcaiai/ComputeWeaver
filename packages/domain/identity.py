from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from typing import NewType, TypeVar

TenantId = NewType("TenantId", str)
SiteId = NewType("SiteId", str)
AssetId = NewType("AssetId", str)
JobId = NewType("JobId", str)
VersionId = NewType("VersionId", str)
PlanId = NewType("PlanId", str)
ActionId = NewType("ActionId", str)

_ID = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
T = TypeVar("T")


def validate_id(value: str) -> str:
    if not _ID.fullmatch(value):
        raise ValueError("identifier must be 3-64 lowercase alphanumeric, '_' or '-'")
    return value


def typed_id(kind: Callable[[str], T], value: str) -> T:
    return kind(validate_id(value))


def new_id(prefix: str) -> str:
    validate_id(prefix)
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def assert_scope(expected_tenant: str, actual_tenant: str, site: str | None = None) -> None:
    if expected_tenant != actual_tenant:
        raise PermissionError("cross-tenant access denied")
    if site is not None:
        validate_id(site)
