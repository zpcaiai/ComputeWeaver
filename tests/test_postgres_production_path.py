from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib import resources

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from apps.api.store import PostgresResourceStore
from packages.approval.workflow import ApprovalRequest, ApprovalStatus, ApprovalWorkflow
from packages.connectors.offsets import ConnectorOffsetStore
from packages.execution.idempotency import IdempotencyStore
from packages.forecasting.registry import ModelRegistry, ModelStage, ModelVersion
from packages.ingestion.normalize import Normalizer
from packages.ingestion.raw import RawEvent, RawLanding
from packages.jobs.queue import PostgresJobQueue
from packages.observability.audit import AuditLog
from packages.persistence.operations import OperationIdempotency
from packages.persistence.postgres import PostgresRuntime
from packages.policy.engine import PolicyEngine
from packages.policy.models import Enforcement, Policy, PolicyRule
from packages.risk.classifier import RiskLevel
from packages.simulation.engine import SimulationConfig
from packages.simulation.session import SimulationRepository
from packages.timeseries.store import TimeSeriesStore
from packages.topology.models import Asset, AssetType
from packages.topology.registry import TopologyRegistry
from packages.workloads.quota import Quota, QuotaLedger


@pytest.mark.integration
def test_postgres_upgrade_from_previous_schema_and_checksum_lock() -> None:
    database_url = os.getenv("COMPUTEWEAVER_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("COMPUTEWEAVER_TEST_DATABASE_URL is not configured")
    connection_parameters = conninfo_to_dict(database_url)
    target_database = f"computeweaver_upgrade_{uuid.uuid4().hex[:16]}"
    admin_parameters = {**connection_parameters, "dbname": connection_parameters.get("dbname", "postgres")}
    target_parameters = {**connection_parameters, "dbname": target_database}
    with psycopg.connect(make_conninfo(**admin_parameters), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_database)))
    previous_runtime = PostgresRuntime(make_conninfo(**target_parameters), min_size=1, max_size=1)
    package = resources.files("packages.persistence.migrations")
    candidates = sorted(
        (item for item in package.iterdir() if item.name.endswith(".sql")),
        key=lambda item: item.name,
    )
    assert len(candidates) >= 2
    try:
        with previous_runtime.connection() as connection:
            connection.execute(
                """
                CREATE TABLE schema_migrations (
                  version bigint PRIMARY KEY,
                  applied_at timestamptz NOT NULL DEFAULT now(),
                  checksum text NOT NULL
                )
                """
            )
            for candidate in candidates[:-1]:
                migration_sql = candidate.read_text(encoding="utf-8")
                version = int(candidate.name.split("_", 1)[0])
                connection.execute(migration_sql)
                connection.execute(
                    "INSERT INTO schema_migrations(version, checksum) VALUES (%s, %s)",
                    (version, hashlib.sha256(migration_sql.encode()).hexdigest()),
                )
        previous_runtime.close()
        upgraded = PostgresRuntime(make_conninfo(**target_parameters), min_size=1, max_size=1)
        try:
            latest_version = int(candidates[-1].name.split("_", 1)[0])
            assert upgraded.migrate() == (latest_version,)
            assert upgraded.migrate() == ()
            assert upgraded.health()
            with upgraded.connection() as connection:
                connection.execute(
                    "UPDATE schema_migrations SET checksum = %s WHERE version = %s",
                    ("0" * 64, latest_version),
                )
            with pytest.raises(RuntimeError, match="checksum changed"):
                upgraded.migrate()
        finally:
            upgraded.close()
    finally:
        previous_runtime.close()
        with psycopg.connect(make_conninfo(**admin_parameters), autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(target_database)))


@pytest.mark.integration
def test_postgres_durability_rls_idempotency_queue_and_workflows() -> None:
    database_url = os.getenv("COMPUTEWEAVER_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("COMPUTEWEAVER_TEST_DATABASE_URL is not configured")
    runtime = PostgresRuntime(database_url, min_size=1, max_size=4)
    runtime.migrate()
    assert runtime.migrate() == ()
    assert runtime.health()
    tenant = f"integration-{uuid.uuid4().hex}"
    now = datetime.now(UTC)
    try:
        store = PostgresResourceStore(runtime)
        first = store.put(
            kind="job",
            resource_id="job-one",
            tenant_id=tenant,
            body={"priority": 50},
            idempotency_key="job-create-0001",
        )
        replay = store.put(
            kind="job",
            resource_id="job-one",
            tenant_id=tenant,
            body={"priority": 50},
            idempotency_key="job-create-0001",
        )
        assert replay == first
        with pytest.raises(ValueError, match="different request"):
            store.put(
                kind="job",
                resource_id="job-one",
                tenant_id=tenant,
                body={"priority": 99},
                idempotency_key="job-create-0001",
            )
        updated = store.put(
            kind="job",
            resource_id="job-one",
            tenant_id=tenant,
            body={"priority": 60},
            idempotency_key="job-update-0001",
            if_match=first.etag,
        )
        assert updated.version == 2
        assert store.get("job", "job-one", tenant).body["priority"] == 60
        assert [item.version for item in store.history("job", "job-one", tenant)] == [1, 2]
        assert store.get_version("job", "job-one", tenant, 1).body["priority"] == 50
        other_tenant = f"integration-{uuid.uuid4().hex}"
        store.put(
            kind="job",
            resource_id="job-one",
            tenant_id=other_tenant,
            body={"priority": 99},
            idempotency_key="job-create-other-0001",
        )
        role_name = f"cw_test_{uuid.uuid4().hex[:16]}"
        with runtime.connection() as connection:
            connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role_name)))
            connection.execute(sql.SQL("GRANT SELECT, INSERT ON resources TO {}").format(sql.Identifier(role_name)))
        with runtime.connection() as connection:
            connection.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role_name)))
            connection.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))
            count = connection.execute("SELECT count(*) AS count FROM resources").fetchone()
            assert count and int(count["count"]) == 1
            with pytest.raises(errors.InsufficientPrivilege), connection.transaction():
                connection.execute(
                    """
                    INSERT INTO resources(tenant_id, kind, resource_id, version, etag, body)
                    VALUES (%s, 'job', 'foreign-write', 1, 'etag', '{}'::jsonb)
                    """,
                    (other_tenant,),
                )

        raw_store = RawLanding(runtime)
        point_store = TimeSeriesStore(runtime)
        raw = RawEvent.create(
            id="meter-0001",
            tenant_id=tenant,
            source="meter",
            received_at=now,
            payload={"metric": "site_power", "timestamp": now.isoformat(), "value": "1000", "unit": "W"},
        )
        assert raw_store.append(raw)
        point = Normalizer().normalize(raw)
        assert point_store.append(point)
        stored_points = point_store.query(
            tenant,
            "site_power",
            now - timedelta(seconds=1),
            now + timedelta(seconds=1),
        )
        assert stored_points[0].value == Decimal(1)
        offsets = ConnectorOffsetStore(runtime)
        offsets.commit(tenant, "meter-one", "intervals", cursor="cursor-two", watermark=now)
        assert offsets.get(tenant, "meter-one", "intervals").cursor == "cursor-two"

        topology = TopologyRegistry(runtime)
        asset = Asset("site-one", tenant, "site-one", AssetType.SITE, "Site", Decimal(100))
        draft_id = topology.create_draft(tenant, (asset,), ())
        assert draft_id.endswith("-1")
        assert topology.publish(tenant, expected_draft_revision=1).version == 1
        assert topology.active(tenant).asset("site-one").capacity_kw == Decimal(100)

        approvals = ApprovalWorkflow(runtime)
        request = ApprovalRequest(
            "approval-one",
            "plan-one",
            tenant,
            RiskLevel.L2,
            "requester",
            now + timedelta(minutes=5),
            frozenset({"safety_admin"}),
            1,
        )
        approvals.create(request)
        approved = approvals.approve(
            request.id,
            actor_id="approver",
            role="safety_admin",
            now=now,
            tenant_id=tenant,
        )
        assert approved.status == ApprovalStatus.APPROVED

        audit = AuditLog(runtime)
        audit.append(
            actor_id="requester",
            tenant_id=tenant,
            action="job.create",
            resource="job-one",
            outcome="success",
            correlation_id="correlation-one",
        )
        audit.append(
            actor_id="approver",
            tenant_id=tenant,
            action="approval.approve",
            resource=request.id,
            outcome="success",
            correlation_id="correlation-two",
        )
        assert audit.verify_tenant(tenant)

        quotas = QuotaLedger(runtime)
        quotas.configure(tenant, Quota(8, Decimal(100), 4))
        assert quotas.reserve(
            tenant,
            2,
            Decimal(4),
            reservation_key="job-quota-one",
        )
        assert quotas.reserve(
            tenant,
            2,
            Decimal(4),
            reservation_key="job-quota-one",
        )
        assert quotas.usage(tenant) == (2, Decimal(4), 1)

        queue = PostgresJobQueue(runtime)
        queued = queue.enqueue(
            tenant_id=tenant,
            kind="heartbeat",
            payload={},
            idempotency_key="heartbeat-0001",
        )
        claimed = queue.claim(worker_id="integration-worker")
        assert claimed is not None and claimed.id == queued
        queue.succeed(claimed, worker_id="integration-worker", result={"ok": True})
        assert queue.status(tenant, queued)["status"] == "succeeded"

        operation_calls = 0

        def operation() -> dict[str, int]:
            nonlocal operation_calls
            operation_calls += 1
            return {"calls": operation_calls}

        operations = OperationIdempotency(runtime)
        operation_result = operations.execute_once(
            tenant_id=tenant,
            key="api-operation-0001",
            operation="policy.publish",
            intent={"version": 1},
            callback=operation,
        )
        operation_replay = operations.execute_once(
            tenant_id=tenant,
            key="api-operation-0001",
            operation="policy.publish",
            intent={"version": 1},
            callback=operation,
        )
        assert operation_result == operation_replay == {"calls": 1}

        action_calls = 0

        def action() -> dict[str, bool]:
            nonlocal action_calls
            action_calls += 1
            return {"executed": True}

        action_store = IdempotencyStore(runtime)
        assert action_store.execute_once(
            "action-idempotency-0001",
            {"target": "simulator"},
            action,
            tenant_id=tenant,
            action_id="action-one",
        ) == {"executed": True}
        assert action_store.execute_once(
            "action-idempotency-0001",
            {"target": "simulator"},
            action,
            tenant_id=tenant,
            action_id="action-one",
        ) == {"executed": True}
        assert action_calls == 1
        assert (
            action_store.cancel(
                "action-cancelled",
                tenant_id=tenant,
                actor_id="requester",
                reason="withdrawn before execution",
            )["status"]
            == "cancelled"
        )
        with pytest.raises(PermissionError, match="cancelled"):
            action_store.execute_once(
                "action-idempotency-cancelled-0001",
                {"target": "simulator"},
                action,
                tenant_id=tenant,
                action_id="action-cancelled",
            )
        with pytest.raises(ValueError, match="succeeded"):
            action_store.cancel(
                "action-one",
                tenant_id=tenant,
                actor_id="requester",
                reason="too late",
            )

        policies = PolicyEngine(runtime)
        policy = Policy(
            "grid-limit",
            1,
            tenant,
            frozenset({"site-one"}),
            PolicyRule("grid_kw", "lte", 100),
            Enforcement.HARD,
            100,
            "safety-owner",
        )
        assert policies.publish(policy, frozenset({"safety_admin"})).published
        assert policies.evaluate(tenant, "site-one", {"grid_kw": 80}).allowed
        assert not policies.evaluate(tenant, "site-one", {"grid_kw": 120}).allowed

        models = ModelRegistry(runtime)
        model_one = ModelVersion("power-forecast", "1", "artifact-one", "dataset-one", now)
        model_two = ModelVersion("power-forecast", "2", "artifact-two", "dataset-two", now)
        models.register(model_one, tenant_id=tenant)
        models.register(model_two, tenant_id=tenant)
        assert models.promote("power-forecast", "1", tenant_id=tenant).stage == ModelStage.PRODUCTION
        assert models.promote("power-forecast", "2", tenant_id=tenant).version == "2"
        assert models.rollback("power-forecast", tenant_id=tenant).version == "1"
        assert models.production("power-forecast", tenant_id=tenant).version == "1"

        simulation = SimulationRepository(store)
        simulation.create(
            "simulation-one",
            tenant,
            SimulationConfig(duration_hours=1, seed=23),
            idempotency_key="simulation-create-0001",
        )
        simulation.operate(
            "simulation-one",
            tenant,
            "step",
            {},
            idempotency_key="simulation-step-0001",
        )
        simulation_snapshot = simulation.operate(
            "simulation-one",
            tenant,
            "snapshot",
            {},
            idempotency_key="simulation-snapshot-0001",
        )
        assert len(str(simulation_snapshot["snapshot_token"])) == 64
        assert (
            simulation.operate(
                "simulation-one",
                tenant,
                "restore",
                {"snapshot_token": simulation_snapshot["snapshot_token"]},
                idempotency_key="simulation-restore-0001",
            )["status"]
            == "restored"
        )

        retry_job_id = queue.enqueue(
            tenant_id=tenant,
            kind="heartbeat",
            payload={"retry": True},
            idempotency_key="heartbeat-retry-0001",
            max_attempts=1,
        )
        retry_job = queue.claim(worker_id="integration-worker")
        assert retry_job is not None and retry_job.id == retry_job_id
        assert queue.heartbeat(retry_job, worker_id="other-worker") is False
        assert queue.heartbeat(retry_job, worker_id="integration-worker") is True
        assert queue.fail(retry_job, worker_id="integration-worker", error="test failure") == "dead_letter"
        assert queue.status(tenant, retry_job_id)["status"] == "dead_letter"

        quotas.release(tenant, 2, Decimal(4), reservation_key="job-quota-one")
        assert quotas.usage(tenant) == (0, Decimal(0), 0)
    finally:
        runtime.close()
