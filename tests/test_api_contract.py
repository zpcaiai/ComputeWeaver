from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from apps.api import main as api_main
from apps.api.main import app
from packages.ingestion.raw import RawEvent

client = TestClient(app)
HEADERS = {
    "X-Tenant-Id": "tenant-one",
    "X-Actor-Id": "user-one",
    "X-Roles": "admin,operator",
    "Idempotency-Key": "idem-12345678",
}


def test_health_version_and_openapi_contract() -> None:
    assert client.get("/health/live").json() == {"status": "live"}
    assert client.get("/health/ready").status_code == 200
    assert client.get("/version").json()["version"] == "0.1.0"
    schema = client.get("/openapi.json").json()
    required_paths = {
        "/v1/topology/drafts",
        "/v1/jobs",
        "/v1/admission/evaluate",
        "/v1/energy/power-balance/validate",
        "/v1/scenario-runs",
        "/v1/optimization-runs",
        "/v1/actions/{action_id}/execute",
        "/v1/multisite/optimize",
        "/v1/certification/{release_id}",
        "/v1/certification/{release_id}/external-readiness",
        "/v1/certification/{release_id}/revoke",
    }
    assert required_paths <= schema["paths"].keys()


def test_readiness_text_fails_closed_when_object_store_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableObjectStore:
        @staticmethod
        def health() -> bool:
            return False

    monkeypatch.setattr(api_main, "object_store_backend", UnavailableObjectStore())
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["object_store"] == "unavailable"


def test_write_requires_auth_and_idempotency() -> None:
    response = client.post("/v1/jobs", json={"id": "job-one", "data": {"priority": 50}})
    assert response.status_code == 401
    incomplete = {key: value for key, value in HEADERS.items() if key != "Idempotency-Key"}
    response = client.post("/v1/jobs", headers=incomplete, json={"id": "job-one", "data": {"priority": 50}})
    assert response.status_code == 422


def test_tenant_admin_can_configure_scoped_quota() -> None:
    headers = {
        "X-Tenant-Id": "tenant-admin-scope",
        "X-Actor-Id": "tenant-admin-one",
        "X-Roles": "tenant_admin",
        "Idempotency-Key": "tenant-quota-0001",
    }
    response = client.put(
        "/v1/quotas/current",
        headers=headers,
        json={
            "id": "tenant-admin-scope",
            "data": {"max_gpus": 8, "max_gpu_hours": "100", "max_concurrent_jobs": 4},
        },
    )
    assert response.status_code == 200
    assert response.json()["max_gpus"] == 8


def test_admin_configuration_history_and_rollback_are_durable_contracts() -> None:
    headers = {
        "X-Tenant-Id": "tenant-config",
        "X-Actor-Id": "tenant-admin-config",
        "X-Roles": "tenant_admin",
        "Idempotency-Key": "config-create-0001",
    }
    first = client.post(
        "/v1/admin/config",
        headers=headers,
        json={"id": "runtime", "data": {"mode": "safe", "max_parallel": 2}},
    )
    assert first.status_code == 200
    second = client.post(
        "/v1/admin/config",
        headers={**headers, "Idempotency-Key": "config-update-0001", "If-Match": first.json()["etag"]},
        json={"id": "runtime", "data": {"mode": "balanced", "max_parallel": 4}},
    )
    assert second.status_code == 200
    versions = client.get("/v1/admin/config/runtime/versions", headers=headers)
    assert [item["version"] for item in versions.json()] == [1, 2]
    rollback = client.post(
        "/v1/admin/config/runtime/rollback",
        headers={**headers, "Idempotency-Key": "config-rollback-0001", "If-Match": second.json()["etag"]},
        json={"target_version": 1},
    )
    assert rollback.status_code == 200
    assert rollback.json()["version"] == 3
    assert rollback.json()["mode"] == "safe"
    assert rollback.json()["rolled_back_from_version"] == 1


def test_compute_connector_test_and_sync_execute_the_real_adapter_contract() -> None:
    headers = {
        "X-Tenant-Id": "tenant-compute-api",
        "X-Actor-Id": "operator-compute-api",
        "X-Roles": "admin,operator",
        "Idempotency-Key": "connector-create-0001",
    }
    connector = client.post(
        "/v1/connectors",
        headers=headers,
        json={
            "id": "simulator-compute",
            "data": {
                "type": "simulator",
                "topology_version": 3,
                "nodes": [
                    {
                        "id": "node-console-one",
                        "site_id": "site-console-one",
                        "topology_asset_id": "rack-console-one",
                        "cpu_cores": 64,
                        "memory_gb": "512",
                        "gpus": [
                            {
                                "id": "gpu-console-one",
                                "model": "H100",
                                "memory_gb": "80",
                                "max_power_kw": "0.7",
                            }
                        ],
                    }
                ],
            },
        },
    )
    assert connector.status_code == 200
    tested = client.post(
        "/v1/connectors/simulator-compute/test",
        headers={**headers, "Idempotency-Key": "connector-test-0001"},
    )
    assert tested.status_code == 200
    assert tested.json()["credentials_valid"] is True
    assert tested.json()["read_only"] is True
    synced = client.post(
        "/v1/connectors/compute/simulator-compute/sync",
        headers={**headers, "Idempotency-Key": "connector-sync-0001"},
    )
    assert synced.status_code == 200
    assert synced.json()["topology_version"] == 3
    nodes = client.get("/v1/compute/nodes", headers=headers).json()
    assert nodes[0]["id"] == "node-console-one"
    assert client.get("/v1/compute/gpus", headers=headers).json()[0]["id"] == "gpu-console-one"


def test_meter_sync_advances_cursor_after_production_ingestion_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = {
        "X-Tenant-Id": "tenant-meter-api",
        "X-Actor-Id": "operator-meter-api",
        "X-Roles": "admin,operator",
        "Idempotency-Key": "meter-create-0001",
    }
    created = client.post(
        "/v1/connectors",
        headers=headers,
        json={
            "id": "meter-api",
            "data": {
                "type": "https_meter",
                "endpoint": "https://meter.example.test",
                "credential_ref": "secret://METER_API",
            },
        },
    )
    assert created.status_code == 200

    class FakeMeter:
        @staticmethod
        def pull(
            *, tenant_id: str, start: datetime, end: datetime, cursor: str | None = None
        ) -> tuple[tuple[RawEvent, ...], str | None]:
            assert end > start
            assert cursor is None
            return (
                (
                    RawEvent.create(
                        id="meter-api:event-one",
                        tenant_id=tenant_id,
                        source="meter-api",
                        received_at=start,
                        payload={
                            "metric": "facility_power",
                            "timestamp": start.isoformat(),
                            "value": "1000",
                            "unit": "W",
                        },
                    ),
                ),
                "cursor-two",
            )

    monkeypatch.setattr(api_main, "create_meter_connector", lambda *_args, **_kwargs: FakeMeter())
    response = client.post(
        "/v1/connectors/meter-api/sync",
        headers={**headers, "Idempotency-Key": "meter-sync-0001"},
        json={
            "id": "meter-api",
            "data": {
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-01T00:15:00+00:00",
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["accepted_raw"] == 1
    assert response.json()["accepted_points"] == 1
    assert response.json()["cursor"] == "cursor-two"


def test_governed_plan_validation_modification_and_approval_update_one_durable_plan() -> None:
    now = datetime.now(UTC)
    requester = {
        "X-Tenant-Id": "tenant-plan-api",
        "X-Actor-Id": "planner-plan-api",
        "X-Roles": "admin,planner",
        "Idempotency-Key": "plan-create-0001",
    }
    created = client.post(
        "/v1/plans",
        headers=requester,
        json={
            "id": "plan-governed-one",
            "data": {
                "site_id": "site-one",
                "state_version": "state-17",
                "policy_versions": ["grid-limit@3"],
                "risk": 2,
                "hard_violations": [],
            },
        },
    )
    assert created.status_code == 200
    validated = client.post(
        "/v1/plans/plan-governed-one/validate",
        headers={
            **requester,
            "Idempotency-Key": "plan-validate-0001",
            "If-Match": created.json()["etag"],
        },
    )
    assert validated.status_code == 200
    assert validated.json()["state"] == "validated"
    submitted = client.post(
        "/v1/plans/plan-governed-one/submit-approval",
        headers={
            **requester,
            "Idempotency-Key": "plan-submit-0001",
            "If-Match": validated.json()["etag"],
        },
        json={
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "required_roles": ["safety_admin"],
            "required_count": 1,
        },
    )
    assert submitted.status_code == 200
    approval_id = submitted.json()["approval"]["id"]
    modified = client.post(
        f"/v1/approvals/{approval_id}/modify",
        headers={**requester, "Idempotency-Key": "approval-modify-0001"},
        json={
            "expires_at": (now + timedelta(hours=2)).isoformat(),
            "required_roles": ["safety_admin"],
            "required_count": 1,
        },
    )
    assert modified.status_code == 200
    approver = {
        "X-Tenant-Id": "tenant-plan-api",
        "X-Actor-Id": "safety-plan-api",
        "X-Roles": "safety_admin",
        "Idempotency-Key": "approval-vote-0001",
    }
    approved = client.post(
        f"/v1/approvals/{approval_id}/approve",
        headers=approver,
        json={"id": approval_id, "data": {"role": "safety_admin", "now": now.isoformat()}},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["plan"]["state"] == "approved"


def test_tariff_publish_recovery_algorithm_and_pre_execution_cancellation() -> None:
    headers = {
        "X-Tenant-Id": "tenant-operations-api",
        "X-Actor-Id": "operator-operations-api",
        "X-Roles": "admin,operator",
        "Idempotency-Key": "tariff-create-0001",
    }
    tariff = client.post(
        "/v1/tariffs",
        headers=headers,
        json={
            "id": "tariff-publish-one",
            "data": {
                "timezone": "UTC",
                "effective_from": "2026-01-01",
                "periods": [
                    {"name": "all-day", "start_local": "00:00:00", "end_local": "00:00:00", "price_per_kwh": "0.2"}
                ],
            },
        },
    )
    published = client.post(
        "/v1/tariffs/tariff-publish-one/publish",
        headers={
            **headers,
            "Idempotency-Key": "tariff-publish-0001",
            "If-Match": tariff.json()["etag"],
        },
    )
    assert published.status_code == 200
    assert published.json()["status"] == "published"
    recovery = client.post(
        "/v1/recovery/plan",
        headers={**headers, "Idempotency-Key": "recovery-plan-0001"},
        json={
            "id": "recovery-one",
            "data": {
                "available_power_kw": "10",
                "loads": [
                    {"id": "critical", "power_kw": "8", "priority": 100, "critical": True},
                    {"id": "batch", "power_kw": "5", "priority": 10, "critical": False},
                ],
            },
        },
    )
    assert recovery.status_code == 200
    assert recovery.json()["served"] == ["critical"]
    cancelled = client.post(
        "/v1/actions/action-cancel-one/cancel",
        headers={**headers, "Idempotency-Key": "action-cancel-0001"},
        json={"id": "action-cancel-one", "data": {"reason": "operator withdrew request"}},
    )
    assert cancelled.status_code == 200
    now = datetime.now(UTC)
    execution = client.post(
        "/v1/actions/action-cancel-one/execute",
        headers={**headers, "Idempotency-Key": "action-execute-0001"},
        json={
            "id": "action-cancel-one",
            "data": {
                "plan_id": "plan-one",
                "target": "simulator",
                "kind": "schedule_job",
                "expected_state_version": "state-one",
                "current_state_version": "state-one",
                "parameters": {"job_id": "job-one"},
                "bounds": {},
                "idempotency_key": "adapter-action-0001",
                "risk": 0,
                "created_at": now.isoformat(),
                "now": now.isoformat(),
            },
        },
    )
    assert execution.status_code == 403
    assert "cancelled" in execution.json()["message"]


def test_scoped_idempotent_resource_and_cross_tenant_isolation() -> None:
    payload = {"id": "job-api-one", "data": {"priority": 50}}
    first = client.post("/v1/jobs", headers=HEADERS, json=payload)
    second = client.post("/v1/jobs", headers=HEADERS, json=payload)
    assert first.status_code == 200
    assert first.json() == second.json()
    other = dict(HEADERS)
    other["X-Tenant-Id"] = "tenant-two"
    other["Idempotency-Key"] = "idem-other-1234"
    assert client.get("/v1/jobs/job-api-one", headers=other).status_code == 404
    created = client.post("/v1/jobs", headers=other, json={"id": "job-api-one", "data": {"priority": 99}})
    assert created.status_code == 200
    assert created.json()["priority"] == 99
    assert client.get("/v1/jobs/job-api-one", headers=HEADERS).json()["priority"] == 50


def test_topology_and_power_balance_api() -> None:
    draft = client.post(
        "/v1/topology/drafts",
        headers={**HEADERS, "Idempotency-Key": "topology-123456"},
        json={
            "assets": [
                {"id": "site-one", "site_id": "site-one", "kind": "site", "capacity_kw": "100"},
                {"id": "rack-one", "site_id": "site-one", "kind": "rack", "capacity_kw": "50"},
            ],
            "relationships": [{"parent_id": "site-one", "child_id": "rack-one"}],
        },
    )
    assert draft.status_code == 200
    publish = client.post(
        "/v1/topology/draft/publish",
        headers={**HEADERS, "Idempotency-Key": "publish-123456"},
        json={"expected_draft_revision": 1},
    )
    assert publish.status_code == 200
    assert client.get("/v1/assets", headers=HEADERS).status_code == 200
    balance = client.post(
        "/v1/energy/power-balance/validate",
        headers={**HEADERS, "Idempotency-Key": "balance-123456"},
        json={
            "compute_load_kw": "50",
            "pue": "1.2",
            "fixed_load_kw": "10",
            "dispatch": {
                "grid_import_kw": "50",
                "grid_export_kw": "0",
                "pv_kw": "20",
                "battery_charge_kw": "0",
                "battery_discharge_kw": "0",
            },
            "grid": {"id": "grid-one", "topology_asset_id": "site-one", "import_limit_kw": "100"},
        },
    )
    assert balance.status_code == 200
    assert balance.json()["violations"] == []


def test_certification_api_is_truthful() -> None:
    response = client.get("/v1/certification/local", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["status"] == "NOT_CERTIFIED"
    readiness = client.get("/v1/certification/local/external-readiness", headers=HEADERS)
    assert readiness.status_code == 200
    assert readiness.json()["status"] != "PASS"
    assert {check["name"] for check in readiness.json()["checks"]} >= {
        "evidence_request",
        "production_preflight",
        "security",
        "performance",
        "backup_restore",
        "external_integrations",
        "acceptance",
    }


def test_cost_api_applies_capacity_export_demand_response_and_tax() -> None:
    response = client.post(
        "/v1/cost/calculate",
        headers={**HEADERS, "Idempotency-Key": "cost-calculate-0001"},
        json={
            "tariff": {
                "id": "tariff-one",
                "timezone": "UTC",
                "effective_from": "2026-01-01",
                "periods": [
                    {"name": "all-day", "start_local": "00:00:00", "end_local": "00:00:00", "price_per_kwh": "0.5"}
                ],
                "demand_charge_per_kw": "2",
                "capacity_charge_per_kw": "1",
                "tax_rate": "0.1",
                "feed_in_price_per_kwh": "0.2",
            },
            "intervals": [
                {
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "import_kwh": "10",
                    "export_kwh": "2",
                    "peak_kw": "4",
                }
            ],
            "contracted_capacity_kw": "3",
            "demand_response_credit": "1",
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert result["capacity"]["amount"] == "3.00"
    assert result["export_credit"]["amount"] == "0.40"
    assert result["total"]["amount"] == "16.06"


def schedule_payload() -> dict[str, object]:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    jobs = []
    for index, priority in enumerate((40, 90), start=1):
        jobs.append(
            {
                "id": f"job-api-{index}",
                "project_id": "project-one",
                "workload_class": "training",
                "request": {
                    "gpu_count": 2,
                    "gpu_model": "H100",
                    "cpu_cores": 8,
                    "memory_gb": "64",
                    "estimated_hours": "1",
                    "power_kw_per_gpu": "0.6",
                },
                "sla": {"deadline": (now + timedelta(hours=4)).isoformat(), "priority": priority},
                "submitted_at": (now + timedelta(minutes=index)).isoformat(),
                "allowed_sites": ["site-one"],
            }
        )
    slots = [
        {
            "index": index,
            "starts_at": (now + timedelta(hours=index)).isoformat(),
            "duration_hours": "1",
            "gpu_capacity": 2,
            "power_capacity_kw": "10",
            "price_per_kwh": price,
        }
        for index, price in enumerate(("0.30", "0.10", "0.20", "0.40"))
    ]
    return {
        "jobs": jobs,
        "slots": slots,
        "topology_version": 1,
        "forecast_version": "forecast-v1",
        "baseline_name": "fifo",
    }


def test_baseline_and_highs_optimization_api_execute_real_algorithms() -> None:
    data = schedule_payload()
    baseline = client.post(
        "/v1/schedules/baseline/price_aware",
        headers={**HEADERS, "Idempotency-Key": "baseline-123456"},
        json={"id": "baseline-api", "data": data},
    )
    assert baseline.status_code == 200
    assert baseline.json()["strategy"] == "price_aware"
    optimization = client.post(
        "/v1/optimization-runs",
        headers={**HEADERS, "Idempotency-Key": "optimizer-123456"},
        json={"id": "optimization-api", "data": data},
    )
    assert optimization.status_code == 200
    assert optimization.json()["status"] == "optimal"
    assert optimization.json()["solver"].startswith("highs-")


def test_action_guard_and_execution_api_are_idempotent() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    data = {
        "plan_id": "plan-one",
        "target": "simulator",
        "kind": "schedule_job",
        "expected_state_version": "state-v1",
        "current_state_version": "state-v1",
        "parameters": {"power_kw": "10"},
        "bounds": {"power_kw": ["0", "20"]},
        "idempotency_key": "action-inner-123456",
        "risk": 0,
        "created_at": now.isoformat(),
        "now": now.isoformat(),
    }
    request = {"id": "action-api", "data": data}
    dry_run = client.post(
        "/v1/actions/action-api/dry-run",
        headers={**HEADERS, "Idempotency-Key": "action-dry-123456"},
        json=request,
    )
    assert dry_run.status_code == 200
    assert dry_run.json()["guard"]["allowed"] is True
    headers = {**HEADERS, "Idempotency-Key": "action-exec-12345"}
    first = client.post("/v1/actions/action-api/execute", headers=headers, json=request)
    second = client.post("/v1/actions/action-api/execute", headers=headers, json=request)
    assert first.status_code == 200
    assert first.json() == second.json()
