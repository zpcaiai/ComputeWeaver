from __future__ import annotations

from typing import Any

from .models import Enforcement, Policy, PolicyRule

SUPPORTED_SCHEMA_VERSION = "1.0"


def parse_policy_document(document: dict[str, Any], *, tenant_id: str) -> Policy:
    if document.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise ValueError("unsupported policy schema version")
    allowed = {
        "schema_version",
        "id",
        "version",
        "site_ids",
        "rule",
        "enforcement",
        "priority",
        "owner",
    }
    unknown = set(document) - allowed
    if unknown:
        raise ValueError(f"unknown policy fields: {sorted(unknown)}")
    rule = document.get("rule")
    if not isinstance(rule, dict):
        raise ValueError("policy rule must be an object")
    operator = str(rule.get("operator"))
    if operator not in {"eq", "ne", "lt", "lte", "gt", "gte", "in", "contains"}:
        raise ValueError("unsupported policy operator")
    return Policy(
        str(document["id"]),
        int(document["version"]),
        tenant_id,
        frozenset(str(item) for item in document.get("site_ids", [])),
        PolicyRule(str(rule["field"]), operator, rule["value"]),
        Enforcement(str(document["enforcement"])),
        int(document["priority"]),
        str(document["owner"]),
    )
