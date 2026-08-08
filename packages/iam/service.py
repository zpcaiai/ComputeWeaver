from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class Identity:
    subject: str
    tenant_id: str
    roles: frozenset[str]
    attributes: dict[str, str]
    token_expires_at: datetime
    service_account: bool = False


@dataclass(frozen=True, slots=True)
class Policy:
    action: str
    allowed_roles: frozenset[str]
    required_attributes: dict[str, str]
    allow_service_accounts: bool = True


def authorize(
    identity: Identity,
    policy: Policy,
    *,
    resource_tenant: str,
    now: datetime,
) -> bool:
    if now >= identity.token_expires_at:
        raise PermissionError("identity token expired")
    if identity.tenant_id != resource_tenant:
        raise PermissionError("cross-tenant access denied")
    if identity.service_account and not policy.allow_service_accounts:
        raise PermissionError("service account is not allowed")
    if not identity.roles.intersection(policy.allowed_roles):
        raise PermissionError("role is not authorized")
    for name, expected in policy.required_attributes.items():
        if identity.attributes.get(name) != expected:
            raise PermissionError(f"attribute {name} does not satisfy policy")
    return True


def validate_sso_claims(claims: dict[str, object], issuer: str, audience: str) -> Identity:
    token_audience = claims.get("aud")
    audience_matches = token_audience == audience or (
        isinstance(token_audience, (list, tuple)) and audience in token_audience
    )
    if claims.get("iss") != issuer or not audience_matches:
        raise PermissionError("OIDC issuer or audience mismatch")
    required = {"sub", "tenant_id", "roles", "exp"}
    if missing := required - claims.keys():
        raise PermissionError(f"missing identity claims {sorted(missing)}")
    roles = claims["roles"]
    attributes = claims.get("attributes", {})
    expires_at = claims["exp"]
    if not isinstance(roles, (list, tuple, set, frozenset)):
        raise PermissionError("roles claim must be a collection")
    if not isinstance(attributes, dict):
        raise PermissionError("attributes claim must be an object")
    if not isinstance(expires_at, (str, int, float)):
        raise PermissionError("exp claim must be numeric")
    return Identity(
        subject=str(claims["sub"]),
        tenant_id=str(claims["tenant_id"]),
        roles=frozenset(str(item) for item in roles),
        attributes={str(k): str(v) for k, v in attributes.items()},
        token_expires_at=datetime.fromtimestamp(float(expires_at), tz=UTC),
        service_account=bool(claims.get("service_account", False)),
    )
