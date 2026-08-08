from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from apps.api.store import StoredResource
from apps.worker.main import Worker, default_handlers
from config.settings import Settings
from packages.certification.evidence import write_evidence
from packages.connectors.meters import HttpsMeterConnector
from packages.connectors.offsets import ConnectorOffsetStore
from packages.connectors.prometheus import PrometheusConnector
from packages.execution.external import GuardedHttpExecutor, ReleaseGate
from packages.jobs.queue import DurableJob, PostgresJobQueue
from packages.objectstore.s3 import S3ObjectStore
from packages.persistence.operations import OperationIdempotency
from packages.scheduling.contracts import ScheduleInput


def _mock_client_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[..., httpx.Client]:
    real_client = httpx.Client

    def factory(**kwargs: Any) -> httpx.Client:
        return real_client(
            base_url=kwargs["base_url"],
            headers=kwargs.get("headers"),
            transport=httpx.MockTransport(handler),
        )

    return factory


def test_prometheus_connector_enforces_allowlist_and_parses_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/query_range"
        assert request.url.params["query"] == "DCGM_FI_DEV_POWER_USAGE"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": {"gpu": "0", "node": "gpu-one"},
                            "values": [[start.timestamp(), "425.5"], ["malformed"]],
                        },
                        "ignored",
                    ]
                },
            },
            request=request,
        )

    monkeypatch.setattr(
        "packages.connectors.prometheus.httpx.Client",
        _mock_client_factory(handler),
    )
    connector = PrometheusConnector(
        base_url="https://prometheus.example.test",
        queries={"gpu_power_w": "DCGM_FI_DEV_POWER_USAGE"},
        token="token",  # noqa: S106
    )
    samples = connector.query_range(
        "gpu_power_w",
        start=start,
        end=start + timedelta(minutes=5),
        step_seconds=30,
    )
    assert samples[0].value == Decimal("425.5")
    assert samples[0].labels["node"] == "gpu-one"
    with pytest.raises(PermissionError, match="allowlist"):
        connector.query_range(
            "arbitrary_query",
            start=start,
            end=start + timedelta(minutes=5),
            step_seconds=30,
        )
    with pytest.raises(ValueError, match="interval"):
        connector.query_range("gpu_power_w", start=start, end=start, step_seconds=0)


def test_prometheus_connector_rejects_insecure_and_malformed_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        PrometheusConnector(base_url="http://prometheus", queries={"power": "up"})
    with pytest.raises(ValueError, match="cannot be empty"):
        PrometheusConnector(base_url="https://prometheus", queries={})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "error"}, request=request)

    monkeypatch.setattr(
        "packages.connectors.prometheus.httpx.Client",
        _mock_client_factory(handler),
    )
    connector = PrometheusConnector(base_url="https://prometheus", queries={"power": "up"})
    now = datetime.now(UTC)
    with pytest.raises(ConnectionError, match="not successful"):
        connector.query_range("power", start=now, end=now + timedelta(minutes=1), step_seconds=15)


def test_https_meter_connector_pulls_cursor_and_stable_raw_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/intervals"
        assert request.url.params["cursor"] == "cursor-one"
        return httpx.Response(
            200,
            json={
                "intervals": [
                    {
                        "metric": "facility_power",
                        "timestamp": start.isoformat(),
                        "value": "2100.25",
                        "unit": "kW",
                        "meter_id": "main-feed",
                    }
                ],
                "next_cursor": "cursor-two",
            },
            request=request,
        )

    monkeypatch.setattr("packages.connectors.meters.httpx.Client", _mock_client_factory(handler))
    connector = HttpsMeterConnector(
        connector_id="meter-api",
        base_url="https://meter.example.test",
        token="token",  # noqa: S106
    )
    events, cursor = connector.pull(
        tenant_id="tenant-one",
        start=start,
        end=start + timedelta(minutes=15),
        cursor="cursor-one",
    )
    assert cursor == "cursor-two"
    assert events[0].tenant_id == "tenant-one"
    assert events[0].payload["unit"] == "kW"
    assert events[0].id.startswith("meter-api:")
    with pytest.raises(ValueError, match="end must follow"):
        connector.pull(tenant_id="tenant-one", start=start, end=start)


def test_https_meter_connector_rejects_insecure_and_malformed_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        HttpsMeterConnector(connector_id="meter", base_url="http://meter")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"intervals": {}, "next_cursor": 7}, request=request)

    monkeypatch.setattr("packages.connectors.meters.httpx.Client", _mock_client_factory(handler))
    connector = HttpsMeterConnector(connector_id="meter", base_url="https://meter")
    now = datetime.now(UTC)
    with pytest.raises(ConnectionError, match="malformed"):
        connector.pull(tenant_id="tenant-one", start=now, end=now + timedelta(minutes=5))


def test_connector_offsets_advance_only_when_explicitly_committed() -> None:
    offsets = ConnectorOffsetStore()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert offsets.get("tenant-one", "meter-one", "intervals").cursor is None
    committed = offsets.commit(
        "tenant-one",
        "meter-one",
        "intervals",
        cursor="cursor-two",
        watermark=now,
    )
    assert committed.cursor == "cursor-two"
    assert offsets.get("tenant-two", "meter-one", "intervals").cursor is None


def _release_settings(tmp_path: Path) -> Settings:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key_file = tmp_path / "release-public.pem"
    public_key_file.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    now = datetime.now(UTC)
    certificate_hash = "a" * 64
    token = jwt.encode(
        {
            "iss": "computeweaver-certifier",
            "aud": "computeweaver-execution",
            "status": "CERTIFIED",
            "commit": "commit-one",
            "release_id": "release-one",
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=5),
            "jti": certificate_hash,
            "certificate_hash": certificate_hash,
            "artifact_set_hash": hashlib.sha256(json.dumps({}, sort_keys=True).encode()).hexdigest(),
        },
        private_key,
        algorithm="RS256",
    )
    revocations_file = tmp_path / "revocations.json"
    write_evidence(
        revocations_file,
        {"status": "PASS", "revocations": []},
        command="test revocation registry",
        suite_name="test-revocations",
    )
    return Settings(
        environment="production",
        executor_target="provider-one",
        executor_url="https://executor.example.test",
        release_public_key_file=str(public_key_file),
        release_revocations_file=str(revocations_file),
        release_commit="commit-one",
        external_write_enabled=True,
        execution_mode="guarded",
        release_certificate=token,
    )


def test_release_gate_and_guarded_executor_require_signed_matching_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _release_settings(tmp_path)
    assert ReleaseGate(settings).validate()["release_id"] == "release-one"

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("dry-run"):
            return httpx.Response(200, json={"valid": True}, request=request)
        return httpx.Response(202, json={"status": "accepted"}, request=request)

    executor = GuardedHttpExecutor(settings)
    monkeypatch.setattr(executor, "_client", lambda: _mock_client_factory(handler)(base_url=settings.executor_url))
    assert executor.dry_run("provider-one", "shed_workload", {"percent": 5})["valid"] is True
    result = executor.execute(
        "provider-one",
        "shed_workload",
        {"percent": 5},
        idempotency_key="action-one",
    )
    assert result["status"] == "accepted"
    assert requests[-1].headers["Idempotency-Key"] == "action-one"
    assert requests[-1].headers["X-ComputeWeaver-Release"] == "release-one"
    with pytest.raises(PermissionError, match="target"):
        executor.dry_run("untrusted-provider", "shed_workload", {})

    mismatched = replace(settings, release_commit="commit-two")
    with pytest.raises(PermissionError, match="does not match"):
        ReleaseGate(mismatched).validate()
    claims = ReleaseGate(settings).validate()
    assert settings.release_revocations_file
    write_evidence(
        Path(settings.release_revocations_file),
        {
            "status": "PASS",
            "revocations": [{"certificate_hash": claims["certificate_hash"]}],
        },
        command="test revoke release",
        suite_name="test-revocations",
    )
    with pytest.raises(PermissionError, match="revoked"):
        ReleaseGate(settings).validate()


def test_release_gate_and_executor_fail_closed_without_configuration(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="feature gate"):
        ReleaseGate(Settings(environment="production")).validate()
    settings = _release_settings(tmp_path)
    missing_url = replace(settings, executor_url=None)
    with pytest.raises(PermissionError, match="HTTPS URL"):
        GuardedHttpExecutor(missing_url)._client()


class _PutResult:
    etag = "etag-one"
    version_id = "version-one"


class _ObjectResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.closed = False
        self.released = False

    def read(self) -> bytes:
        return self.content

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class _ObjectClient:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.exists = True

    def bucket_exists(self, _: str) -> bool:
        return self.exists

    def put_object(self, _: str, key: str, stream: Any, **__: Any) -> _PutResult:
        self.objects[key] = cast(bytes, stream.read())
        return _PutResult()

    def get_object(self, _: str, key: str) -> _ObjectResponse:
        return _ObjectResponse(self.objects[key])


def _object_store(client: _ObjectClient) -> S3ObjectStore:
    store = object.__new__(S3ObjectStore)
    store.bucket = "computeweaver"
    store.client = cast(Any, client)
    return store


def test_object_store_tenant_prefix_digest_and_health() -> None:
    client = _ObjectClient()
    store = _object_store(client)
    info = store.put("tenant-one", "models/model.bin", b"artifact", content_type="application/octet-stream")
    assert info.key == "tenants/tenant-one/models/model.bin"
    assert info.version_id == "version-one"
    assert store.get("tenant-one", "models/model.bin", expected_sha256=info.sha256) == b"artifact"
    assert store.health() is True
    with pytest.raises(RuntimeError, match="digest"):
        store.get("tenant-one", "models/model.bin", expected_sha256="0" * 64)
    with pytest.raises(ValueError, match="empty"):
        store.put("tenant-one", "empty.bin", b"", content_type="application/octet-stream")
    with pytest.raises(ValueError, match="tenant"):
        S3ObjectStore._key("bad/tenant", "object")
    with pytest.raises(ValueError, match="object key"):
        S3ObjectStore._key("tenant-one", "../secret")


class _BrokenObjectClient(_ObjectClient):
    def bucket_exists(self, _: str) -> bool:
        raise ConnectionError("unavailable")


def test_object_store_configuration_and_unavailable_health() -> None:
    with pytest.raises(ValueError, match="s3"):
        S3ObjectStore(
            bucket_url="file:///tmp/objects",
            endpoint="https://s3.example.test",
            access_key="access",
            secret_key="secret",  # noqa: S106
        )
    with pytest.raises(ValueError, match="HTTP"):
        S3ObjectStore(
            bucket_url="s3://computeweaver",
            endpoint="ftp://s3.example.test",
            access_key="access",
            secret_key="secret",  # noqa: S106
        )
    assert _object_store(_BrokenObjectClient()).health() is False


class _Queue:
    def __init__(self, job: DurableJob | None) -> None:
        self.job = job
        self.succeeded: list[dict[str, Any]] = []
        self.failed: list[str] = []

    def claim(self, *, worker_id: str) -> DurableJob | None:
        assert worker_id == "worker-one"
        job, self.job = self.job, None
        return job

    def succeed(
        self,
        _: DurableJob,
        *,
        worker_id: str,
        result: dict[str, Any],
    ) -> None:
        assert worker_id == "worker-one"
        self.succeeded.append(result)

    def fail(self, _: DurableJob, *, worker_id: str, error: str) -> str:
        assert worker_id == "worker-one"
        self.failed.append(error)
        return "pending"


def _job(kind: str = "heartbeat") -> DurableJob:
    return DurableJob(
        1,
        "tenant-one",
        kind,
        {},
        "job-one",
        1,
        3,
        datetime.now(UTC) + timedelta(minutes=1),
    )


def test_worker_records_success_idle_and_failure() -> None:
    async def success(job: DurableJob) -> dict[str, Any]:
        return {"job_id": job.id}

    queue = _Queue(_job())
    worker = Worker(cast(PostgresJobQueue, queue), worker_id="worker-one", handlers={"heartbeat": success})
    assert asyncio.run(worker.run_once()) is True
    assert queue.succeeded == [{"job_id": 1}]
    assert worker.processed == 1
    assert asyncio.run(worker.run_once()) is False

    failed_queue = _Queue(_job("unknown"))
    failed_worker = Worker(cast(PostgresJobQueue, failed_queue), worker_id="worker-one", handlers={})
    assert asyncio.run(failed_worker.run_once()) is True
    assert failed_queue.failed == ["KeyError: 'unknown'"]


class _Store:
    def put(self, **kwargs: Any) -> StoredResource:
        assert kwargs["idempotency_key"] == "job:job-one"
        return StoredResource("plans", "plan-one", "tenant-one", 2, "etag-two", kwargs["body"])


def test_default_worker_handlers_cover_persistent_resource_job() -> None:
    async def scenario() -> None:
        handlers = default_handlers(cast(Any, _Store()))
        heartbeat: dict[str, Any] = await handlers["heartbeat"](_job())
        assert heartbeat == {"status": "ok", "job_id": 1}
        job = DurableJob(
            2,
            "tenant-one",
            "resource_put",
            {"kind": "plans", "resource_id": "plan-one", "body": {"state": "draft"}},
            "job-one",
            1,
            3,
            datetime.now(UTC) + timedelta(minutes=1),
        )
        assert await handlers["resource_put"](job) == {
            "resource_id": "plan-one",
            "version": 2,
            "etag": "etag-two",
        }

    asyncio.run(scenario())


def test_api_operation_idempotency_is_tenant_scoped_and_retries_failures() -> None:
    operations = OperationIdempotency()
    calls = 0

    def execute() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    first = operations.execute_once(
        tenant_id="tenant-one",
        key="operation-0001",
        operation="topology.publish",
        intent={"revision": 1},
        callback=execute,
    )
    replay = operations.execute_once(
        tenant_id="tenant-one",
        key="operation-0001",
        operation="topology.publish",
        intent={"revision": 1},
        callback=execute,
    )
    other_tenant = operations.execute_once(
        tenant_id="tenant-two",
        key="operation-0001",
        operation="topology.publish",
        intent={"revision": 1},
        callback=execute,
    )
    assert first == replay == {"calls": 1}
    assert other_tenant == {"calls": 2}
    with pytest.raises(ValueError, match="different"):
        operations.execute_once(
            tenant_id="tenant-one",
            key="operation-0001",
            operation="topology.publish",
            intent={"revision": 2},
            callback=execute,
        )

    attempts = 0

    def flaky() -> dict[str, bool]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("temporary")
        return {"recovered": True}

    with pytest.raises(ConnectionError):
        operations.execute_once(
            tenant_id="tenant-one",
            key="operation-0002",
            operation="connector.sync",
            intent={},
            callback=flaky,
        )
    assert operations.execute_once(
        tenant_id="tenant-one",
        key="operation-0002",
        operation="connector.sync",
        intent={},
        callback=flaky,
    ) == {"recovered": True}


def test_worker_optimization_handler_executes_real_solver(schedule_input: ScheduleInput) -> None:
    handlers = default_handlers(cast(Any, _Store()))
    job = DurableJob(
        3,
        "tenant-one",
        "optimization_run",
        {"schedule": asdict(schedule_input), "timeout_seconds": 2},
        "optimization-job-one",
        1,
        3,
        datetime.now(UTC) + timedelta(minutes=1),
    )

    async def scenario() -> dict[str, Any]:
        return await handlers["optimization_run"](job)

    result = asyncio.run(scenario())
    assert result["status"] == "optimal"
    assert str(result["solver"]).startswith("highs-")
