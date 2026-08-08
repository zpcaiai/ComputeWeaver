from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from config.settings import Settings
from packages.connectors.kubernetes import KubernetesAdapter
from packages.connectors.slurm import SlurmAdapter
from packages.iam.authentication import AuthenticationError, Authenticator


def test_oidc_authenticator_verifies_asymmetric_signature_and_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": "key-one", "use": "sig", "alg": "RS256"})

    def jwks(_: str, **__: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"keys": [jwk]},
            request=httpx.Request("GET", "https://id.example.test/jwks"),
        )

    monkeypatch.setattr("packages.iam.authentication.httpx.get", jwks)
    settings = Settings(
        environment="production",
        database_url="postgresql://db/computeweaver",
        object_store="s3://computeweaver",
        auth_mode="oidc",
        oidc_issuer="https://id.example.test/",
        oidc_audience="computeweaver",
        oidc_jwks_url="https://id.example.test/jwks",
    )
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": settings.oidc_issuer,
            "aud": settings.oidc_audience,
            "sub": "user-one",
            "tenant_id": "tenant-one",
            "roles": ["operator"],
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "key-one"},
    )
    identity = Authenticator(settings).authenticate(
        authorization=f"Bearer {token}",
        trusted_headers={},
    )
    assert identity.subject == "user-one"
    assert identity.tenant_id == "tenant-one"
    assert identity.roles == frozenset({"operator"})


def test_oidc_authenticator_rejects_unsigned_token() -> None:
    settings = Settings(
        environment="production",
        database_url="postgresql://db/computeweaver",
        object_store="s3://computeweaver",
        auth_mode="oidc",
        oidc_issuer="https://id.example.test/",
        oidc_audience="computeweaver",
        oidc_jwks_url="https://id.example.test/jwks",
    )
    unsigned = jwt.encode(
        {"sub": "attacker", "tenant_id": "tenant-one", "roles": ["admin"]},
        key="",
        algorithm="none",
    )
    with pytest.raises(AuthenticationError, match="signing method"):
        Authenticator(settings).authenticate(
            authorization=f"Bearer {unsigned}",
            trusted_headers={},
        )


def test_production_settings_fail_closed_without_oidc_and_postgres() -> None:
    settings = Settings(
        environment="production",
        database_url="memory://",
        object_store="file:///tmp/objects",
        auth_mode="trusted_headers",
    )
    with pytest.raises(ValueError):
        settings.validate()


def test_production_settings_require_tls_for_every_control_plane_dependency() -> None:
    base = Settings(
        environment="production",
        database_url="postgresql://user:secret@db.example.test/computeweaver?sslmode=verify-full",
        object_store="s3://computeweaver",
        object_store_endpoint="https://objects.example.test",
        object_store_access_key="access",
        object_store_secret_key="secret",  # noqa: S106
        auth_mode="oidc",
        oidc_issuer="https://id.example.test/",
        oidc_audience="computeweaver",
        oidc_jwks_url="https://id.example.test/jwks",
        otlp_endpoint="https://otel.example.test",
    )
    base.validate()
    with pytest.raises(ValueError, match="PostgreSQL requires TLS"):
        replace(base, database_url="postgresql://user:secret@db/computeweaver").validate()
    with pytest.raises(ValueError, match="OTLP"):
        replace(base, otlp_endpoint="http://otel:4318").validate()
    with pytest.raises(ValueError, match="OIDC"):
        replace(base, oidc_issuer="http://id.example.test").validate()


def test_live_kubernetes_adapter_normalizes_api_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/nodes"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "metadata": {
                            "name": "gpu-node-one",
                            "uid": "node-uid-one",
                            "labels": {
                                "computeweaver.io/tenant": "tenant-one",
                                "topology.kubernetes.io/region": "site-one",
                                "nvidia.com/gpu.product": "H100",
                                "computeweaver.io/gpu-memory-gb": "80",
                                "computeweaver.io/gpu-max-power-kw": "0.7",
                            },
                        },
                        "status": {
                            "capacity": {"cpu": "32", "memory": "256Gi", "nvidia.com/gpu": "2"},
                            "conditions": [{"type": "Ready", "status": "True"}],
                        },
                    }
                ],
                "metadata": {},
            },
            request=request,
        )

    adapter = KubernetesAdapter(
        "kubernetes",
        api_url="https://kube.example.test",
        token="token",  # noqa: S106
    )
    monkeypatch.setattr(
        adapter,
        "_client",
        lambda: httpx.Client(
            base_url="https://kube.example.test",
            transport=httpx.MockTransport(handler),
        ),
    )
    nodes = adapter.discover()
    assert len(nodes) == 1
    assert nodes[0].tenant_id == "tenant-one"
    assert len(nodes[0].gpus) == 2
    assert nodes[0].gpus[0].model == "H100"


def test_live_slurm_adapter_normalizes_slurmrestd_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/nodes")
        return httpx.Response(
            200,
            json={
                "nodes": [
                    {
                        "name": "slurm-node-one",
                        "cluster": "site-one",
                        "cpus": 64,
                        "real_memory": 262144,
                        "gres": "gpu:H100:4",
                        "state": ["IDLE"],
                        "gpu_memory_gb": 80,
                        "gpu_max_power_kw": "0.7",
                    }
                ]
            },
            request=request,
        )

    adapter = SlurmAdapter(
        "slurm",
        api_url="https://slurm.example.test",
        jwt_token="token",  # noqa: S106
        tenant_id="tenant-one",
    )
    monkeypatch.setattr(
        adapter,
        "_client",
        lambda: httpx.Client(
            base_url="https://slurm.example.test",
            transport=httpx.MockTransport(handler),
        ),
    )
    nodes = adapter.discover()
    assert nodes[0].site_id == "site-one"
    assert len(nodes[0].gpus) == 4
    assert nodes[0].memory_gb == 256
