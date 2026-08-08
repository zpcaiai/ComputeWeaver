from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Enforcement(StrEnum):
    HARD = "hard"
    SOFT = "soft"


@dataclass(frozen=True, slots=True)
class PolicyRule:
    field: str
    operator: str
    value: Any


@dataclass(frozen=True, slots=True)
class Policy:
    id: str
    version: int
    tenant_id: str
    site_ids: frozenset[str]
    rule: PolicyRule
    enforcement: Enforcement
    priority: int
    owner: str
    published: bool = False
