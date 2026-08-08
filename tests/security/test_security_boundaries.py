from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from apps.api.main import reject_inline_secrets
from config.settings import Settings
from packages.iam.authentication import forwarded_peer_certificate
from packages.objectstore.s3 import S3ObjectStore
from packages.secrets import CredentialResolver


def test_forwarded_certificate_is_ignored_without_authenticated_proxy() -> None:
    assert (
        forwarded_peer_certificate(
            direct_hash=None,
            forwarded_hash="attacker-certificate",
            supplied_proxy_secret="wrong",  # noqa: S106
            configured_proxy_secret="expected",  # noqa: S106
        )
        is None
    )
    assert (
        forwarded_peer_certificate(
            direct_hash="direct-certificate",
            forwarded_hash="attacker-certificate",
            supplied_proxy_secret=None,
            configured_proxy_secret=None,
        )
        == "direct-certificate"
    )


def test_production_never_accepts_trusted_identity_headers_or_tls_disable() -> None:
    base = Settings(
        environment="production",
        database_url="postgresql://user:secret@db/computeweaver?sslmode=require",
        object_store="s3://objects",
        object_store_endpoint="https://objects.example.test",
        object_store_access_key="access",
        object_store_secret_key="secret",  # noqa: S106
        auth_mode="trusted_headers",
    )
    with pytest.raises(ValueError, match="trusted header"):
        base.validate()
    with pytest.raises(ValueError, match="TLS verification"):
        replace(
            base,
            auth_mode="oidc",
            oidc_issuer="https://id.example.test",
            oidc_audience="computeweaver",
            oidc_jwks_url="https://id.example.test/jwks",
            external_write_enabled=True,
            execution_mode="guarded",
            release_certificate="certificate",  # noqa: S106
            release_public_key_file="/missing",
            release_commit="commit",
            executor_target="gateway",
            executor_url="https://gateway.example.test",
            executor_ca_bundle=False,
        ).validate()


def test_object_keys_cannot_escape_tenant_prefix() -> None:
    assert S3ObjectStore._key("tenant-one", "reports/a.json") == "tenants/tenant-one/reports/a.json"
    with pytest.raises(ValueError):
        S3ObjectStore._key("tenant-one", "../../other-tenant/secret")


def test_persisted_resource_bodies_reject_inline_secret_material() -> None:
    reject_inline_secrets({"endpoint": "https://provider", "credential_ref": "secret/provider-token"})
    with pytest.raises(ValueError, match="credential_ref"):
        reject_inline_secrets({"endpoint": "https://provider", "api_token": "plaintext"})


def test_connector_secret_resolver_cannot_read_arbitrary_environment_or_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "connector-secrets"
    root.mkdir()
    (root / "slurm.jwt").write_text("signed-token\n", encoding="utf-8")
    resolver = CredentialResolver(root)
    monkeypatch.setenv("COMPUTEWEAVER_CONNECTOR_SECRET_CLUSTER_ONE", "environment-token")

    assert resolver.resolve("secret://CLUSTER_ONE") == "environment-token"
    assert resolver.resolve("file://slurm.jwt") == "signed-token"
    with pytest.raises(ValueError, match="secret name"):
        resolver.resolve("secret://../DATABASE_URL")
    with pytest.raises(PermissionError, match="escapes"):
        resolver.resolve("file://../outside")
    with pytest.raises(ValueError, match="credential_ref"):
        resolver.resolve("env://COMPUTEWEAVER_DATABASE_URL")
