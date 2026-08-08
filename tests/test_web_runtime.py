from __future__ import annotations

from pathlib import Path

import pytest

from apps.web.main import WebHandler, WebSettings


def test_development_web_config_exposes_only_public_identity_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "index.html").write_text("<div id='app'></div>", encoding="utf-8")
    monkeypatch.setenv("COMPUTEWEAVER_ENV", "simulator")
    monkeypatch.setenv("COMPUTEWEAVER_WEB_STATIC_ROOT", str(tmp_path))
    monkeypatch.setenv("COMPUTEWEAVER_WEB_API_UPSTREAM", "http://api:8000")
    monkeypatch.setenv("COMPUTEWEAVER_WEB_DEV_TENANT", "tenant-one")
    settings = WebSettings.from_env()

    document = settings.public_config()
    assert document["environment"] == "simulator"
    assert document["dev_identity"] == {
        "tenant_id": "tenant-one",
        "actor_id": "operator-local",
        "roles": "admin,operator",
    }
    assert "api_upstream" not in document


def test_production_web_config_requires_https_oidc_and_public_client_id(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<div id='app'></div>", encoding="utf-8")
    base = WebSettings(
        environment="production",
        host="127.0.0.1",
        port=8080,
        static_root=tmp_path,
        api_upstream="http://computeweaver-api",
        oidc_issuer=None,
        oidc_client_id=None,
        oidc_audience="computeweaver",
        oidc_scopes="openid profile",
        dev_identity=None,
    )
    with pytest.raises(ValueError, match="HTTPS OIDC"):
        base.validate()
    with pytest.raises(ValueError, match="client ID"):
        WebSettings(
            environment="production",
            host=base.host,
            port=base.port,
            static_root=base.static_root,
            api_upstream=base.api_upstream,
            oidc_issuer="https://identity.example.test",
            oidc_client_id=None,
            oidc_audience=base.oidc_audience,
            oidc_scopes=base.oidc_scopes,
            dev_identity=None,
        ).validate()


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/health/ready", True),
        ("/version", True),
        ("/v1/jobs", True),
        ("/openapi.json", True),
        ("/web-config.json", False),
        ("/../../etc/passwd", False),
    ],
)
def test_web_proxy_has_an_explicit_allowlist(path: str, expected: bool) -> None:
    assert WebHandler._is_proxy_path(path) is expected
