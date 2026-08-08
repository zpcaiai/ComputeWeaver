from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import httpx

from config.settings import Settings
from packages.connectors.factory import create_compute_adapter, create_meter_connector
from packages.execution.external import GuardedHttpExecutor
from packages.iam.authentication import Authenticator
from packages.secrets import CredentialResolver


@dataclass(frozen=True, slots=True)
class ExternalCheck:
    name: str
    status: str
    checked_at: datetime
    details: dict[str, Any]
    reason: str | None = None


def _check(name: str, operation: Callable[[], dict[str, Any]]) -> ExternalCheck:
    now = datetime.now(UTC)
    try:
        return ExternalCheck(name, "PASS", now, operation())
    except (httpx.HTTPError, OSError, PermissionError, RuntimeError, ValueError, KeyError) as error:
        return ExternalCheck(name, "FAIL", now, {}, f"{type(error).__name__}: {error}")


def _oidc(
    configuration: dict[str, Any], resolver: CredentialResolver, expected_tenant_id: str | None = None
) -> dict[str, Any]:
    issuer = str(configuration["issuer"]).rstrip("/")
    audience = str(configuration["audience"])
    jwks_url = str(configuration["jwks_url"])
    discovery_url = str(configuration.get("discovery_url", f"{issuer}/.well-known/openid-configuration"))
    if not all(value.startswith("https://") for value in (issuer, jwks_url, discovery_url)):
        raise ValueError("OIDC issuer, discovery and JWKS URLs must use HTTPS")
    response = httpx.get(discovery_url, timeout=10, follow_redirects=False)
    response.raise_for_status()
    discovery = response.json()
    if not isinstance(discovery, dict) or discovery.get("issuer") != issuer or discovery.get("jwks_uri") != jwks_url:
        raise ValueError("OIDC discovery metadata does not match the configured trust anchors")
    token = resolver.resolve(str(configuration["token_ref"]))
    settings = Settings(auth_mode="oidc", oidc_issuer=issuer, oidc_audience=audience, oidc_jwks_url=jwks_url)
    identity = Authenticator(settings).authenticate(
        authorization=f"Bearer {token}",
        trusted_headers={},
        peer_certificate_sha256=(
            str(configuration["peer_certificate_sha256"]) if configuration.get("peer_certificate_sha256") else None
        ),
    )
    if expected_tenant_id and identity.tenant_id != expected_tenant_id:
        raise PermissionError("OIDC token tenant does not match the acceptance tenant")
    required_roles = {str(role) for role in configuration.get("required_roles", [])}
    if not required_roles.issubset(identity.roles):
        raise PermissionError("OIDC token is missing a required acceptance role")
    return {
        "issuer": issuer,
        "subject": identity.subject,
        "tenant_id": identity.tenant_id,
        "roles": sorted(identity.roles),
        "token_expiry": identity.token_expires_at.isoformat(),
    }


def _compute(name: str, configuration: dict[str, Any], tenant_id: str, resolver: CredentialResolver) -> dict[str, Any]:
    adapter = create_compute_adapter(name, configuration, tenant_id=tenant_id, resolver=resolver)
    if adapter.read_only is not True:
        raise PermissionError(f"{name} acceptance adapter must be read-only")
    if not adapter.validate_credentials():
        raise PermissionError(f"{name} credential probe failed")
    nodes = adapter.discover()
    if not nodes:
        raise RuntimeError(f"{name} returned no compute nodes")
    if any(node.tenant_id != tenant_id for node in nodes):
        raise PermissionError(f"{name} returned a node outside the configured tenant")
    return {
        "read_only": adapter.read_only,
        "node_count": len(nodes),
        "gpu_count": sum(len(node.gpus) for node in nodes),
        "sites": sorted({node.site_id for node in nodes}),
    }


def _meter(configuration: dict[str, Any], tenant_id: str, resolver: CredentialResolver) -> dict[str, Any]:
    connector = create_meter_connector("production-meter", configuration, resolver=resolver)
    if not connector.probe():
        raise PermissionError("meter credential probe failed")
    end = datetime.now(UTC)
    start = end - timedelta(minutes=int(configuration.get("sample_minutes", 15)))
    events, cursor = connector.pull(tenant_id=tenant_id, start=start, end=end)
    if not events:
        raise RuntimeError("meter returned no samples in the acceptance window")
    if any(getattr(event, "tenant_id", tenant_id) != tenant_id for event in events):
        raise PermissionError("meter returned a sample outside the configured tenant")
    return {"sample_count": len(events), "cursor_returned": cursor is not None, "window_end": end.isoformat()}


def _ems(configuration: dict[str, Any], resolver: CredentialResolver) -> dict[str, Any]:
    target = str(configuration["target"])
    token_ref = configuration.get("credential_ref")
    certificate_ref = configuration.get("client_certificate_ref")
    key_ref = configuration.get("client_key_ref")
    if bool(certificate_ref) != bool(key_ref):
        raise ValueError("EMS mTLS certificate and key references must be configured together")
    settings = Settings(
        environment="production",
        executor_target=target,
        executor_url=str(configuration["endpoint"]),
        executor_token=resolver.resolve(str(token_ref)) if token_ref else None,
        executor_ca_bundle=(
            str(resolver.resolve_file(str(configuration["ca_bundle_ref"])))
            if configuration.get("ca_bundle_ref")
            else True
        ),
        executor_client_certificate=(str(resolver.resolve_file(str(certificate_ref))) if certificate_ref else None),
        executor_client_key=str(resolver.resolve_file(str(key_ref))) if key_ref else None,
    )
    result = GuardedHttpExecutor(settings).dry_run(
        target,
        str(configuration.get("probe_action", "set_power_limit")),
        dict(configuration.get("probe_parameters", {"power_kw": 0, "validation_only": True})),
    )
    return {"target": target, "provider_validation": result, "external_write_executed": False}


def run_external_acceptance(manifest: dict[str, Any], *, resolver: CredentialResolver | None = None) -> dict[str, Any]:
    resolver = resolver or CredentialResolver.from_env()
    tenant_id = str(manifest.get("tenant_id", ""))
    if not tenant_id:
        raise ValueError("external acceptance manifest requires tenant_id")
    operations: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
        "oidc": lambda config: _oidc(config, resolver, tenant_id),
        "kubernetes": lambda config: _compute("kubernetes", config, tenant_id, resolver),
        "slurm": lambda config: _compute("slurm", config, tenant_id, resolver),
        "meter": lambda config: _meter(config, tenant_id, resolver),
        "ems": lambda config: _ems(config, resolver),
    }
    checks: list[ExternalCheck] = []
    for name, operation in operations.items():
        raw = manifest.get(name)
        if not isinstance(raw, dict):
            checks.append(ExternalCheck(name, "NOT_RUN", datetime.now(UTC), {}, "configuration missing"))
            continue
        checks.append(_check(name, partial(operation, raw)))
    statuses = {item.status for item in checks}
    overall = "FAIL" if "FAIL" in statuses else "PASS" if statuses == {"PASS"} else "NOT_RUN"
    return {
        "status": overall,
        "release_id": manifest.get("release_id"),
        "source_revision": manifest.get("source_revision"),
        "request_sha256": manifest.get("request_sha256"),
        "external_writes_executed": False,
        "checks": [asdict(item) for item in checks],
    }


def load_manifest(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("external acceptance manifest must be a JSON object")
    return document
