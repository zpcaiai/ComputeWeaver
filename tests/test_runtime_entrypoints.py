from __future__ import annotations

import http.client
import json
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn

from apps.api.cli import main as api_main
from apps.simulator.main import main as simulator_main
from apps.web import main as web_runtime
from packages.objectstore import cli as objectstore_cli
from packages.persistence import cli as persistence_cli


def test_api_and_simulator_entrypoints_execute_real_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["computeweaver", "version"])
    api_main()
    assert json.loads(capsys.readouterr().out)["version"] == "0.1.0"
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(sys, "argv", ["computeweaver", "serve"])
    api_main()
    assert calls[0][0] == ("apps.api.main:app",)

    monkeypatch.setattr(sys, "argv", ["simulator", "--hours", "1", "--seed", "13"])
    simulator_main()
    events = json.loads(capsys.readouterr().out)
    assert len(events) == 4


def test_persistence_cli_rejects_memory_and_closes_runtime(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    memory = SimpleNamespace(in_memory_mode=True)
    monkeypatch.setattr(persistence_cli.Settings, "from_env", lambda: memory)
    monkeypatch.setattr(sys, "argv", ["persistence", "check"])
    with pytest.raises(SystemExit, match="reject memory"):
        persistence_cli.main()

    settings = SimpleNamespace(
        in_memory_mode=False,
        database_url="postgresql://example",
        database_pool_max=2,
        database_connect_timeout_seconds=1,
    )

    class Runtime:
        closed = False

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def migrate(self) -> tuple[int, ...]:
            return (1, 2)

        def health(self) -> bool:
            return True

        def close(self) -> None:
            Runtime.closed = True

    monkeypatch.setattr(persistence_cli.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(persistence_cli, "PostgresRuntime", Runtime)
    monkeypatch.setattr(sys, "argv", ["persistence", "migrate"])
    persistence_cli.main()
    assert json.loads(capsys.readouterr().out) == {"applied": [1, 2], "healthy": True}
    assert Runtime.closed


def test_object_store_initializer_creates_versioned_bucket(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = SimpleNamespace(
        object_store="s3://computeweaver",
        object_store_endpoint="http://objects:9000",
        object_store_access_key="access",
        object_store_secret_key="test-only-secret",  # noqa: S106 - non-production fake
        object_store_ca_bundle=None,
    )
    calls: list[str] = []

    class Client:
        def bucket_exists(self, bucket: str) -> bool:
            calls.append(f"exists:{bucket}")
            return False

        def make_bucket(self, bucket: str) -> None:
            calls.append(f"create:{bucket}")

        def set_bucket_versioning(self, bucket: str, _configuration: object) -> None:
            calls.append(f"version:{bucket}")

    class Store:
        bucket = "computeweaver"
        client = Client()

        def __init__(self, **_kwargs: object) -> None:
            pass

        def health(self) -> bool:
            return True

    monkeypatch.setattr(objectstore_cli.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(objectstore_cli, "S3ObjectStore", Store)
    objectstore_cli.main()
    assert "object store ready" in capsys.readouterr().out
    assert calls == ["exists:computeweaver", "create:computeweaver", "version:computeweaver"]


def test_web_handler_serves_static_config_proxy_and_rejects_bad_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "index.html").write_text("<main>ComputeWeaver</main>", encoding="utf-8")
    (tmp_path / "asset.js").write_text("export default true", encoding="utf-8")
    settings = web_runtime.WebSettings(
        environment="test",
        host="127.0.0.1",
        port=8080,
        static_root=tmp_path,
        api_upstream="http://api:8000",
        oidc_issuer="https://identity.example.com",
        oidc_client_id="client-one",
        oidc_audience="computeweaver",
        oidc_scopes="openid",
        dev_identity={"tenant_id": "tenant-one", "actor_id": "operator-one", "roles": "operator"},
        release_id="release-one",
        release_commit="a" * 40,
    )

    class ProxyClient:
        def __init__(self, **_kwargs: object) -> None:
            assert _kwargs["trust_env"] is False

        def __enter__(self) -> ProxyClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def request(self, method: str, path: str, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                status_code=200,
                headers={"Content-Type": "application/json", "Connection": "close"},
                content=json.dumps({"method": method, "path": path}).encode(),
            )

    class Handler(web_runtime.WebHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

    Handler.settings = settings
    monkeypatch.setattr(web_runtime.httpx, "Client", ProxyClient)
    server = web_runtime.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = server.socket.getsockname()
    host = str(address[0])
    port = int(address[1])

    def request(method: str, path: str, headers: dict[str, str] | None = None) -> http.client.HTTPResponse:
        connection = http.client.HTTPConnection(host, port, timeout=2)
        connection.request(method, path, headers=headers or {})
        return connection.getresponse()

    try:
        health = request("GET", "/web-health/live")
        assert json.loads(health.read())["status"] == "live"
        assert health.headers["X-Frame-Options"] == "DENY"
        config = json.loads(request("GET", "/web-config.json").read())
        assert config["dev_identity"]["tenant_id"] == "tenant-one"
        assert config["release_commit"] == "a" * 40
        static = request("GET", "/asset.js")
        assert static.headers["Cache-Control"].startswith("public")
        assert b"export default" in static.read()
        fallback = request("GET", "/client-side-route")
        assert b"ComputeWeaver" in fallback.read()
        proxied = json.loads(request("GET", "/v1/jobs?limit=1").read())
        assert proxied["path"] == "/v1/jobs?limit=1"

        connection = http.client.HTTPConnection(host, port, timeout=2)
        connection.request("DELETE", "/admin/internal")
        assert connection.getresponse().status == 404
        connection.close()
        connection = http.client.HTTPConnection(host, port, timeout=2)
        connection.putrequest("POST", "/v1/jobs")
        connection.putheader("Content-Length", "invalid")
        connection.endheaders()
        assert connection.getresponse().status == 400
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_web_settings_fail_closed_on_invalid_runtime_configuration(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("ok", encoding="utf-8")
    valid = web_runtime.WebSettings(
        environment="test",
        host="127.0.0.1",
        port=8080,
        static_root=tmp_path,
        api_upstream="http://api:8000",
        oidc_issuer=None,
        oidc_client_id=None,
        oidc_audience=None,
        oidc_scopes="openid",
        dev_identity=None,
    )
    with pytest.raises(ValueError, match="unknown"):
        replace(valid, environment="invalid").validate()
    with pytest.raises(ValueError, match="port"):
        replace(valid, port=0).validate()
    with pytest.raises(ValueError, match="upstream"):
        replace(valid, api_upstream="file:///tmp/api").validate()
    with pytest.raises(ValueError, match="static build"):
        replace(valid, static_root=tmp_path / "missing").validate()
