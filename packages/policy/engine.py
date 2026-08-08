from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from packages.persistence.postgres import PostgresRuntime

from .models import Enforcement, Policy, PolicyRule


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    hard_violations: tuple[str, ...]
    warnings: tuple[str, ...]
    active_policies: tuple[str, ...]
    explanation: tuple[str, ...]


def evaluate_rule(rule: PolicyRule, facts: dict[str, Any]) -> bool:
    if rule.field not in facts:
        return False
    actual = facts[rule.field]
    operations = {
        "eq": lambda: actual == rule.value,
        "ne": lambda: actual != rule.value,
        "lt": lambda: actual < rule.value,
        "lte": lambda: actual <= rule.value,
        "gt": lambda: actual > rule.value,
        "gte": lambda: actual >= rule.value,
        "in": lambda: actual in rule.value,
        "contains": lambda: rule.value in actual,
    }
    try:
        return bool(operations[rule.operator]())
    except KeyError as error:
        raise ValueError(f"unsupported policy operator {rule.operator}") from error


class PolicyEngine:
    def __init__(self, runtime: PostgresRuntime | None = None) -> None:
        self._runtime = runtime
        self._policies: dict[tuple[str, int], Policy] = {}

    @staticmethod
    def _from_row(row: dict[str, Any]) -> Policy:
        rule = row["rule"]
        return Policy(
            str(row["policy_id"]),
            int(row["version"]),
            str(row["tenant_id"]),
            frozenset(str(item) for item in row["site_ids"]),
            PolicyRule(str(rule["field"]), str(rule["operator"]), rule["value"]),
            Enforcement(str(row["enforcement"])),
            int(row["priority"]),
            str(row["owner_id"]),
            bool(row["published"]),
        )

    def _tenant_policies(self, tenant_id: str) -> tuple[Policy, ...]:
        if not self._runtime:
            return tuple(item for item in self._policies.values() if item.tenant_id == tenant_id)
        with self._runtime.tenant_connection(tenant_id) as connection:
            rows = connection.execute(
                "SELECT * FROM policy_versions WHERE tenant_id = %s ORDER BY priority DESC, policy_id, version",
                (tenant_id,),
            ).fetchall()
            return tuple(self._from_row(row) for row in rows)

    @staticmethod
    def _validate_against(policy: Policy, existing: tuple[Policy, ...]) -> None:
        for other in existing:
            if (
                other.published
                and other.tenant_id == policy.tenant_id
                and other.site_ids & policy.site_ids
                and other.rule.field == policy.rule.field
                and other.priority == policy.priority
                and other.rule.operator == "eq"
                and policy.rule.operator == "eq"
                and other.rule.value != policy.rule.value
            ):
                raise ValueError(f"policy conflict with {other.id}@{other.version}")

    def validate_publish(self, policy: Policy) -> None:
        self._validate_against(policy, self._tenant_policies(policy.tenant_id))

    def publish(self, policy: Policy, actor_roles: frozenset[str]) -> Policy:
        if policy.enforcement == Enforcement.HARD and "safety_admin" not in actor_roles:
            raise PermissionError("only safety_admin can publish hard policy")
        published = Policy(
            policy.id,
            policy.version,
            policy.tenant_id,
            policy.site_ids,
            policy.rule,
            policy.enforcement,
            policy.priority,
            policy.owner,
            True,
        )
        if self._runtime:
            with self._runtime.tenant_connection(policy.tenant_id, policy.owner) as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 17))",
                    (policy.tenant_id,),
                )
                rows = connection.execute(
                    "SELECT * FROM policy_versions WHERE tenant_id = %s",
                    (policy.tenant_id,),
                ).fetchall()
                self._validate_against(policy, tuple(self._from_row(row) for row in rows))
                connection.execute(
                    """
                    INSERT INTO policy_versions(
                      tenant_id, policy_id, version, site_ids, rule, enforcement,
                      priority, owner_id, published
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true)
                    """,
                    (
                        policy.tenant_id,
                        policy.id,
                        policy.version,
                        list(policy.site_ids),
                        Jsonb(
                            {
                                "field": policy.rule.field,
                                "operator": policy.rule.operator,
                                "value": policy.rule.value,
                            }
                        ),
                        policy.enforcement.value,
                        policy.priority,
                        policy.owner,
                    ),
                )
            return published
        self.validate_publish(policy)
        self._policies[(policy.id, policy.version)] = published
        return published

    def evaluate(self, tenant_id: str, site_id: str, facts: dict[str, Any]) -> PolicyDecision:
        active = sorted(
            (
                item
                for item in self._tenant_policies(tenant_id)
                if item.published and item.tenant_id == tenant_id and (not item.site_ids or site_id in item.site_ids)
            ),
            key=lambda item: (-item.priority, item.id),
        )
        hard: list[str] = []
        warnings: list[str] = []
        explanation: list[str] = []
        for policy in active:
            matched = evaluate_rule(policy.rule, facts)
            explanation.append(f"{policy.id}@{policy.version}:{'matched' if matched else 'failed'}")
            if not matched:
                target = hard if policy.enforcement == Enforcement.HARD else warnings
                target.append(policy.id)
        return PolicyDecision(
            not hard,
            tuple(hard),
            tuple(warnings),
            tuple(f"{item.id}@{item.version}" for item in active),
            tuple(explanation),
        )
