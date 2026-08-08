from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import Body, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from apps.api.store import PostgresResourceStore, ResourceStore, Store, StoredResource
from apps.worker.ingestion import IngestionProcessor
from config.settings import Settings
from packages.admission.service import AdmissionService
from packages.approval.workflow import ApprovalRequest, ApprovalStatus, ApprovalWorkflow
from packages.benchmark.runner import benchmark
from packages.certification.lifecycle import CertificationRepository
from packages.certification.service import GateResult, certify_release
from packages.compute.inventory import ComputeNode, Gpu
from packages.compute.snapshot import SnapshotBuilder
from packages.connectors.factory import create_compute_adapter, create_meter_connector
from packages.connectors.offsets import ConnectorOffsetStore
from packages.energy.assets import GridConnection
from packages.energy.power_balance import Dispatch, validate_power_balance
from packages.execution.action_guard import Action, ActionGuard
from packages.execution.adapters import SimulatorExecutor
from packages.execution.external import GuardedHttpExecutor
from packages.execution.idempotency import IdempotencyStore
from packages.explain.service import explain_plan
from packages.forecasting.models import ObservedValue, PersistenceModel
from packages.forecasting.registry import ModelRegistry, ModelVersion
from packages.iam.authentication import (
    AuthenticationError,
    Authenticator,
    forwarded_peer_certificate,
)
from packages.ingestion.normalize import Normalizer
from packages.ingestion.raw import RawLanding
from packages.island.planner import plan_island_survival
from packages.jobs.queue import PostgresJobQueue
from packages.mpc.repository import MpcRepository
from packages.mpc.state_estimator import ObservedState
from packages.multisite.model import Site, SiteLink
from packages.multisite.optimizer import evaluate_migration
from packages.objectstore.s3 import S3ObjectStore
from packages.observability.audit import AuditLog
from packages.observability.telemetry import configure_telemetry
from packages.optimization.solvers import HighsSolver
from packages.persistence.operations import OperationIdempotency
from packages.persistence.postgres import PostgresRuntime
from packages.plans.diff import compare as compare_plans
from packages.policy.engine import PolicyEngine
from packages.policy.models import Enforcement, Policy, PolicyRule
from packages.replay.service import replay
from packages.reports.service import build_savings_report
from packages.resilience.planner import CriticalLoad, plan_degraded_mode
from packages.risk.classifier import RiskLevel
from packages.scenarios.compiler import compile_scenario, run_scenario
from packages.scheduling.serialization import parse_job, parse_schedule_input, parse_schedule_plan
from packages.scheduling.strategies import (
    schedule_fifo,
    schedule_price_aware,
    schedule_priority_edf,
)
from packages.simulation.engine import SimulationConfig
from packages.simulation.session import SimulationRepository
from packages.tariffs.calculator import MeterInterval, TariffCalculator
from packages.tariffs.models import PricePeriod, TariffPlan
from packages.timeseries.store import TimeSeriesStore
from packages.topology.models import Asset, AssetType, Relationship
from packages.topology.registry import TopologyRegistry
from packages.whatif.service import run_what_if
from packages.workloads.quota import Quota, QuotaLedger

VERSION = "0.1.0"


class ApiContext(BaseModel):
    tenant_id: str
    actor_id: str
    roles: frozenset[str]
    correlation_id: str
    idempotency_key: str | None = None
    if_match: str | None = None


def read_context(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
    x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
    x_roles: Annotated[str | None, Header(alias="X-Roles")] = None,
    x_correlation_id: Annotated[str | None, Header(alias="X-Correlation-Id")] = None,
) -> ApiContext:
    direct_certificate = request.scope.get("client_cert_sha256")
    certificate = forwarded_peer_certificate(
        direct_hash=str(direct_certificate) if direct_certificate else None,
        forwarded_hash=request.headers.get("X-Client-Cert-SHA256"),
        supplied_proxy_secret=request.headers.get("X-ComputeWeaver-Proxy-Secret"),
        configured_proxy_secret=settings.trusted_proxy_secret,
    )
    identity = authenticator.authenticate(
        authorization=authorization,
        trusted_headers={"tenant_id": x_tenant_id, "actor_id": x_actor_id, "roles": x_roles},
        peer_certificate_sha256=certificate,
    )
    request.state.identity = identity
    if x_tenant_id and x_tenant_id != identity.tenant_id:
        raise PermissionError("tenant header does not match authenticated identity")
    _authorize_request(request.method, request.url.path, identity.roles)
    return ApiContext(
        tenant_id=identity.tenant_id,
        actor_id=identity.subject,
        roles=identity.roles,
        correlation_id=x_correlation_id or str(uuid.uuid4()),
    )


def write_context(
    context: Annotated[ApiContext, Depends(read_context)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ApiContext:
    return context.model_copy(update={"idempotency_key": idempotency_key, "if_match": if_match})


ReadContext = Annotated[ApiContext, Depends(read_context)]
WriteContext = Annotated[ApiContext, Depends(write_context)]


class ResourceRequest(BaseModel):
    id: str
    data: dict[str, Any] = Field(default_factory=dict)


class TopologyDraftRequest(BaseModel):
    assets: list[dict[str, Any]]
    relationships: list[dict[str, Any]]


class PublishRequest(BaseModel):
    expected_draft_revision: int = Field(ge=1)


class PowerBalanceRequest(BaseModel):
    compute_load_kw: Decimal
    pue: Decimal
    fixed_load_kw: Decimal
    dispatch: dict[str, Decimal]
    grid: dict[str, Any]


class ConfigRollbackRequest(BaseModel):
    target_version: int = Field(ge=1)


class CertificationRevocationRequest(BaseModel):
    reason: str = Field(min_length=8, max_length=1000)


class PlanCompareRequest(BaseModel):
    candidate_id: str


class ApprovalSubmissionRequest(BaseModel):
    expires_at: datetime
    required_roles: frozenset[str]
    required_count: int = Field(ge=0)


def _authorize_request(method: str, path: str, roles: frozenset[str]) -> None:
    if not roles:
        raise PermissionError("identity has no assigned role")
    if path.startswith(("/v1/admin", "/v1/tenants", "/v1/users", "/v1/roles")):
        allowed = {"admin", "tenant_admin"}
    elif path.endswith("/execute"):
        allowed = {"admin", "operator", "executor"}
    elif method in {"POST", "PUT", "PATCH", "DELETE"}:
        allowed = {"admin", "tenant_admin", "operator", "planner", "model_admin", "safety_admin"}
    else:
        allowed = {
            "admin",
            "tenant_admin",
            "operator",
            "viewer",
            "auditor",
            "planner",
            "model_admin",
            "safety_admin",
        }
    if not roles.intersection(allowed):
        raise PermissionError("role is not authorized for this operation")


settings = Settings.from_env()
certification_repository = CertificationRepository(Path(settings.certification_evidence_root))
runtime: PostgresRuntime | None = None
if settings.in_memory_mode:
    store: Store = ResourceStore()
else:
    runtime = PostgresRuntime(
        settings.database_url,
        min_size=settings.database_pool_min,
        max_size=settings.database_pool_max,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    store = PostgresResourceStore(runtime)
object_store_backend = (
    S3ObjectStore(
        bucket_url=settings.object_store,
        endpoint=cast(str, settings.object_store_endpoint),
        access_key=cast(str, settings.object_store_access_key),
        secret_key=cast(str, settings.object_store_secret_key),
        ca_bundle=settings.object_store_ca_bundle,
    )
    if all(
        (
            settings.object_store.startswith("s3://"),
            settings.object_store_endpoint,
            settings.object_store_access_key,
            settings.object_store_secret_key,
        )
    )
    else None
)
audit = AuditLog(runtime)
authenticator = Authenticator(settings)
topologies = TopologyRegistry(runtime)
raw_landing = RawLanding(runtime)
timeseries = TimeSeriesStore(runtime)
connector_offsets = ConnectorOffsetStore(runtime)
ingestion_processor = IngestionProcessor(raw_landing, timeseries)
policy_engine = PolicyEngine(runtime)
model_registry = ModelRegistry(runtime)
approval_workflow = ApprovalWorkflow(runtime)
execution_idempotency = IdempotencyStore(runtime)
api_operations = OperationIdempotency(runtime)
simulator_executor = SimulatorExecutor()
guarded_executor = GuardedHttpExecutor(settings)
job_queue = PostgresJobQueue(runtime) if runtime else None
quota_ledger = QuotaLedger(runtime)
simulation_repository = SimulationRepository(store)
mpc_repository = MpcRepository(store)


@asynccontextmanager
async def lifespan(_: FastAPI) -> Any:
    settings.validate()
    if runtime:
        runtime.open()
        if settings.migration_on_startup:
            runtime.migrate()
    yield
    if runtime:
        runtime.close()


app = FastAPI(
    title="ComputeWeaver API",
    version=VERSION,
    description="AI compute, energy and infrastructure control plane",
    lifespan=lifespan,
)
telemetry = configure_telemetry(app, settings)


@app.middleware("http")
async def correlation_middleware(request: Request, call_next: Any) -> Response:
    correlation_id = request.headers.get("X-Correlation-Id") or str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Correlation-Id"] = correlation_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    response.headers["Cache-Control"] = "no-store"
    if settings.environment == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def error_response(request: Request, status: int, code: str, message: str) -> JSONResponse:
    correlation_id = request.headers.get("X-Correlation-Id") or str(uuid.uuid4())
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "message": message,
            "correlation_id": correlation_id,
            "details": [],
            "retryable": status >= 500,
        },
    )


@app.exception_handler(PermissionError)
async def permission_error(request: Request, error: PermissionError) -> JSONResponse:
    return error_response(request, 403, "FORBIDDEN", str(error))


@app.exception_handler(AuthenticationError)
async def authentication_error(request: Request, error: AuthenticationError) -> JSONResponse:
    response = error_response(request, 401, "UNAUTHENTICATED", str(error))
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


@app.exception_handler(KeyError)
async def not_found_error(request: Request, error: KeyError) -> JSONResponse:
    return error_response(request, 404, "NOT_FOUND", str(error))


@app.exception_handler(ValueError)
async def value_error(request: Request, error: ValueError) -> JSONResponse:
    return error_response(request, 422, "VALIDATION_ERROR", str(error))


@app.exception_handler(RuntimeError)
async def conflict_error(request: Request, error: RuntimeError) -> JSONResponse:
    return error_response(request, 409, "CONFLICT", str(error))


@app.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "live"}


@app.get("/health/ready")
def health_ready() -> JSONResponse:
    try:
        settings.validate()
    except ValueError as error:
        return JSONResponse(status_code=503, content={"status": "not_ready", "reason": str(error)})
    database_ready = store.health()
    object_store_ready = (
        object_store_backend.health()
        if object_store_backend
        else settings.environment in {"test", "simulator", "development"}
    )
    ready = database_ready and object_store_ready
    status = 200 if ready else 503
    return JSONResponse(
        status_code=status,
        content={
            "status": "ready" if ready else "not_ready",
            "version": VERSION,
            "database": "ready" if database_ready else "unavailable",
            "object_store": "ready" if object_store_ready else "unavailable",
            "telemetry_export": telemetry.otlp_export_enabled,
        },
    )


@app.get("/version")
def version() -> dict[str, str]:
    return {"name": "computeweaver", "version": VERSION}


@app.post("/v1/system/jobs")
def enqueue_system_job(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    if not job_queue:
        raise RuntimeError("durable queue is unavailable in memory mode")
    kind = str(body.data["kind"])
    if kind not in {"resource_put", "heartbeat", "optimization_run"}:
        raise ValueError("unsupported durable job kind")
    payload = dict(body.data.get("payload", {}))

    def enqueue() -> dict[str, Any]:
        job_id = job_queue.enqueue(
            tenant_id=context.tenant_id,
            kind=kind,
            payload=payload,
            idempotency_key=context.idempotency_key or body.id,
            max_attempts=int(body.data.get("max_attempts", 5)),
        )
        return {"job_id": job_id, "status": "pending"}

    return audited_operation(
        context,
        operation="durable_job.enqueue",
        resource=body.id,
        intent={"kind": kind, "payload": payload, "max_attempts": body.data.get("max_attempts", 5)},
        callback=enqueue,
    )


@app.get("/v1/system/jobs/{job_id}")
def durable_job_status(job_id: int, context: ReadContext) -> dict[str, Any]:
    if not job_queue:
        raise RuntimeError("durable queue is unavailable in memory mode")
    return job_queue.status(context.tenant_id, job_id)


@app.put("/v1/artifacts/{artifact_key:path}")
def put_artifact(
    artifact_key: str,
    context: WriteContext,
    content: Annotated[bytes, Body(max_length=50 * 1024 * 1024)],
    content_type: Annotated[str, Header(alias="Content-Type")],
) -> dict[str, Any]:
    if not object_store_backend:
        raise RuntimeError("object storage is not configured")
    content_sha256 = hashlib.sha256(content).hexdigest()

    def persist() -> dict[str, Any]:
        info = object_store_backend.put(
            context.tenant_id,
            artifact_key,
            content,
            content_type=content_type,
        )
        return mutate(
            "artifact",
            ResourceRequest(
                id=artifact_key,
                data={
                    "bucket": info.bucket,
                    "key": info.key,
                    "etag": info.etag,
                    "version_id": info.version_id,
                    "sha256": info.sha256,
                    "size": info.size,
                    "content_type": content_type,
                },
            ),
            context,
        )

    return audited_operation(
        context,
        operation="artifact.put",
        resource=artifact_key,
        intent={"sha256": content_sha256, "content_type": content_type},
        callback=persist,
    )


@app.get("/v1/artifacts/{artifact_key:path}")
def get_artifact(artifact_key: str, context: ReadContext) -> Response:
    if not object_store_backend:
        raise RuntimeError("object storage is not configured")
    metadata = store.get("artifact", artifact_key, context.tenant_id).body
    content = object_store_backend.get(
        context.tenant_id,
        artifact_key,
        expected_sha256=str(metadata["sha256"]),
    )
    return Response(
        content=content,
        media_type=str(metadata["content_type"]),
        headers={"ETag": str(metadata["etag"]), "X-Content-SHA256": str(metadata["sha256"])},
    )


def serialize(resource: StoredResource) -> dict[str, Any]:
    return {
        **resource.body,
        "version": resource.version,
        "etag": resource.etag,
        "tenant_id": resource.tenant_id,
    }


def reject_inline_secrets(value: Any, path: str = "body") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized != "credential_ref" and (
                normalized in {"password", "secret", "token", "private_key", "credentials"}
                or normalized.endswith(("_password", "_secret", "_token", "_private_key"))
            ):
                raise ValueError(f"inline secret material is prohibited at {path}.{key}; use credential_ref")
            reject_inline_secrets(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_inline_secrets(item, f"{path}[{index}]")


def mutate(kind: str, body: ResourceRequest, context: ApiContext) -> dict[str, Any]:
    reject_inline_secrets(body.data)
    resource = store.put(
        kind=kind,
        resource_id=body.id,
        tenant_id=context.tenant_id,
        body=body.data,
        idempotency_key=context.idempotency_key or "missing",
        if_match=context.if_match,
    )
    audit.append(
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action=f"{kind}.write",
        resource=body.id,
        outcome="success",
        correlation_id=context.correlation_id,
    )
    return serialize(resource)


def audited_operation(
    context: ApiContext,
    *,
    operation: str,
    resource: str,
    intent: dict[str, Any],
    callback: Callable[[], Any],
) -> Any:
    def execute() -> Any:
        result = callback()
        audit.append(
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action=operation,
            resource=resource,
            outcome="success",
            correlation_id=context.correlation_id,
        )
        return result

    return api_operations.execute_once(
        tenant_id=context.tenant_id,
        key=context.idempotency_key or "missing-idempotency-key",
        operation=operation,
        intent=intent,
        callback=execute,
        actor_id=context.actor_id,
    )


def list_kind(kind: str, context: ApiContext) -> list[dict[str, Any]]:
    return [serialize(item) for item in store.list(kind, context.tenant_id)]


@app.post("/v1/topology/drafts")
def create_topology_draft(body: TopologyDraftRequest, context: WriteContext) -> dict[str, Any]:
    assets = tuple(
        Asset(
            id=str(item["id"]),
            tenant_id=context.tenant_id,
            site_id=str(item["site_id"]),
            kind=AssetType(item["kind"]),
            name=str(item.get("name", item["id"])),
            capacity_kw=Decimal(str(item.get("capacity_kw", 0))),
            attributes={str(k): str(v) for k, v in dict(item.get("attributes", {})).items()},
        )
        for item in body.assets
    )
    relationships = tuple(
        Relationship(str(item["parent_id"]), str(item["child_id"]), str(item.get("kind", "contains")))
        for item in body.relationships
    )

    def create() -> dict[str, Any]:
        draft_id = topologies.create_draft(context.tenant_id, assets, relationships)
        return {"id": draft_id, "revision": int(draft_id.rsplit("-", 1)[1]), "valid": True}

    return audited_operation(
        context,
        operation="topology.draft.create",
        resource=context.tenant_id,
        intent={"assets": body.assets, "relationships": body.relationships},
        callback=create,
    )


@app.post("/v1/topology/{version}/publish")
def publish_topology(version: str, body: PublishRequest, context: WriteContext) -> dict[str, Any]:
    def publish() -> dict[str, Any]:
        snapshot = topologies.publish(context.tenant_id, expected_draft_revision=body.expected_draft_revision)
        return {
            "version": snapshot.version,
            "etag": snapshot.etag,
            "published_at": snapshot.published_at,
        }

    return audited_operation(
        context,
        operation="topology.publish",
        resource=version,
        intent={"version": version, "expected_draft_revision": body.expected_draft_revision},
        callback=publish,
    )


@app.get("/v1/assets")
def get_assets(context: ReadContext) -> list[dict[str, Any]]:
    snapshot = topologies.active(context.tenant_id)
    return [asdict(item) for item in snapshot.assets]


@app.get("/v1/assets/{asset_id}")
def get_asset(asset_id: str, context: ReadContext) -> dict[str, Any]:
    return asdict(topologies.active(context.tenant_id).asset(asset_id))


@app.get("/v1/topology/graph")
def get_topology_graph(context: ReadContext) -> dict[str, Any]:
    snapshot = topologies.active(context.tenant_id)
    return {
        "version": snapshot.version,
        "assets": [asdict(item) for item in snapshot.assets],
        "relationships": [asdict(item) for item in snapshot.relationships],
    }


@app.get("/v1/topology/versions")
def topology_versions(context: ReadContext) -> list[dict[str, Any]]:
    return [
        {
            "version": snapshot.version,
            "published_at": snapshot.published_at,
            "etag": snapshot.etag,
            "asset_count": len(snapshot.assets),
            "relationship_count": len(snapshot.relationships),
        }
        for snapshot in topologies.versions(context.tenant_id)
    ]


@app.get("/v1/audit/records")
def audit_records(context: ReadContext) -> list[dict[str, Any]]:
    if not context.roles.intersection({"admin", "auditor"}):
        raise PermissionError("audit records require auditor role")
    return [asdict(record) for record in audit.records(context.tenant_id)]


@app.get("/v1/audit/integrity")
def audit_integrity(context: ReadContext) -> dict[str, Any]:
    if not context.roles.intersection({"admin", "auditor"}):
        raise PermissionError("audit integrity requires auditor role")
    records = audit.records(context.tenant_id)
    return {
        "valid": audit.verify_tenant(context.tenant_id),
        "record_count": len(records),
        "head": records[-1].record_hash if records else "GENESIS",
    }


@app.post("/v1/energy/power-balance/validate")
def power_balance(body: PowerBalanceRequest, context: WriteContext) -> dict[str, Any]:
    del context
    grid = GridConnection(
        id=str(body.grid["id"]),
        topology_asset_id=str(body.grid["topology_asset_id"]),
        import_limit_kw=Decimal(str(body.grid["import_limit_kw"])),
        export_limit_kw=Decimal(str(body.grid.get("export_limit_kw", 0))),
    )
    result = validate_power_balance(
        compute_load_kw=body.compute_load_kw,
        pue=body.pue,
        fixed_load_kw=body.fixed_load_kw,
        dispatch=Dispatch(**body.dispatch),
        grid=grid,
    )
    return asdict(result)


@app.post("/v1/connectors/{connector_id}/sync")
def sync_connector(connector_id: str, body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    if connector_id != body.id:
        raise ValueError("connector ID mismatch")
    configuration = store.get("connector", connector_id, context.tenant_id)
    stream = str(body.data.get("stream", "intervals"))
    if not stream or len(stream) > 120:
        raise ValueError("connector stream is invalid")
    offset = connector_offsets.get(context.tenant_id, connector_id, stream)
    start_value = body.data.get("start") or offset.watermark
    end_value = body.data.get("end")
    if not start_value or not end_value:
        raise ValueError("connector sync requires start and end timestamps")
    start = start_value if isinstance(start_value, datetime) else datetime.fromisoformat(str(start_value))
    end = end_value if isinstance(end_value, datetime) else datetime.fromisoformat(str(end_value))
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("connector sync timestamps must be timezone-aware")

    def synchronize() -> dict[str, Any]:
        connector = create_meter_connector(connector_id, configuration.body)
        events, next_cursor = connector.pull(
            tenant_id=context.tenant_id,
            start=start,
            end=end,
            cursor=offset.cursor,
        )
        result = ingestion_processor.ingest(events)
        committed = connector_offsets.commit(
            context.tenant_id,
            connector_id,
            stream,
            cursor=next_cursor,
            watermark=end,
        )
        return {
            **asdict(result),
            "connector_id": connector_id,
            "stream": stream,
            "cursor": committed.cursor,
            "watermark": committed.watermark,
        }

    return audited_operation(
        context,
        operation="connector.sync",
        resource=connector_id,
        intent={
            "connector_version": configuration.version,
            "stream": stream,
            "start": start,
            "end": end,
            "cursor": offset.cursor,
        },
        callback=synchronize,
    )


@app.get("/v1/timeseries/query")
def query_timeseries(metric: str, start: datetime, end: datetime, context: ReadContext) -> list[dict[str, Any]]:
    return [asdict(item) for item in timeseries.query(context.tenant_id, metric, start, end)]


@app.post("/v1/simulations")
def create_simulation(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    def create() -> dict[str, Any]:
        config = SimulationConfig(
            seed=int(body.data.get("seed", 7)),
            duration_hours=int(body.data.get("duration_hours", 24)),
            step_minutes=int(body.data.get("step_minutes", 15)),
            gpu_count=int(body.data.get("gpu_count", 16)),
        )
        return simulation_repository.create(
            body.id,
            context.tenant_id,
            config,
            idempotency_key=context.idempotency_key or "missing-idempotency-key",
        )

    return audited_operation(
        context,
        operation="simulation.create",
        resource=body.id,
        intent=body.data,
        callback=create,
    )


@app.post("/v1/simulations/{simulation_id}/{operation}")
def operate_simulation(
    simulation_id: str,
    operation: str,
    context: WriteContext,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    def operate() -> dict[str, Any]:
        return simulation_repository.operate(
            simulation_id,
            context.tenant_id,
            operation,
            body,
            idempotency_key=context.idempotency_key or "missing-idempotency-key",
        )

    return audited_operation(
        context,
        operation=f"simulation.{operation}",
        resource=simulation_id,
        intent=body,
        callback=operate,
    )


@app.post("/v1/scenarios/validate")
def validate_scenario(body: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    del context
    scenario = compile_scenario(body)
    return {"valid": True, "scenario": asdict(scenario)}


@app.post("/v1/scenario-runs")
def scenario_run(body: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    del context
    scenario = compile_scenario(body)
    events, evaluation = run_scenario(scenario)
    return {"event_count": len(events), "evaluation": evaluation.as_dict()}


@app.post("/v1/cost/calculate")
def calculate_cost(body: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    del context
    periods = tuple(
        PricePeriod(
            str(item["name"]),
            time.fromisoformat(str(item["start_local"])),
            time.fromisoformat(str(item["end_local"])),
            Decimal(str(item["price_per_kwh"])),
            frozenset(int(day) for day in item.get("weekdays", range(7))),
        )
        for item in body["tariff"]["periods"]
    )
    plan = TariffPlan(
        id=str(body["tariff"]["id"]),
        version=int(body["tariff"].get("version", 1)),
        currency=str(body["tariff"].get("currency", "USD")),
        timezone=str(body["tariff"]["timezone"]),
        effective_from=date.fromisoformat(str(body["tariff"]["effective_from"])),
        effective_to=(
            date.fromisoformat(str(body["tariff"]["effective_to"])) if body["tariff"].get("effective_to") else None
        ),
        periods=periods,
        demand_charge_per_kw=Decimal(str(body["tariff"].get("demand_charge_per_kw", 0))),
        capacity_charge_per_kw=Decimal(str(body["tariff"].get("capacity_charge_per_kw", 0))),
        tax_rate=Decimal(str(body["tariff"].get("tax_rate", 0))),
        feed_in_price_per_kwh=Decimal(str(body["tariff"].get("feed_in_price_per_kwh", 0))),
    )
    intervals = tuple(
        MeterInterval(
            datetime.fromisoformat(str(item["started_at"])),
            Decimal(str(item.get("duration_hours", 1))),
            Decimal(str(item["import_kwh"])),
            Decimal(str(item.get("export_kwh", 0))),
            Decimal(str(item.get("peak_kw", 0))),
        )
        for item in body["intervals"]
    )
    return asdict(
        TariffCalculator().calculate(
            plan,
            intervals,
            contracted_capacity_kw=Decimal(str(body.get("contracted_capacity_kw", 0))),
            demand_response_credit=Decimal(str(body.get("demand_response_credit", 0))),
            demand_response_penalty=Decimal(str(body.get("demand_response_penalty", 0))),
        )
    )


@app.post("/v1/cost/compare")
def compare_cost(body: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    left = calculate_cost(dict(body["baseline"]), context)
    right = calculate_cost(dict(body["candidate"]), context)
    left_total = Decimal(str(left["total"]["amount"]))
    right_total = Decimal(str(right["total"]["amount"]))
    return {"baseline": left, "candidate": right, "savings": left_total - right_total}


# CRUD-like contracts that use the same scoped, idempotent and auditable repository.
GENERIC_COLLECTIONS = {
    "/v1/jobs": "job",
    "/v1/reservations": "reservation",
    "/v1/tariffs": "tariff",
    "/v1/energy/assets": "energy_asset",
    "/v1/connectors": "connector",
    "/v1/mpc/controllers": "mpc_controller",
    "/v1/tenants": "tenant",
    "/v1/users": "user",
    "/v1/roles": "role",
    "/v1/budgets": "budget",
    "/v1/notifications/routes": "notification_route",
    "/v1/admin/connectors": "admin_connector",
    "/v1/admin/models": "admin_model",
    "/v1/admin/solvers": "admin_solver",
    "/v1/emergency/events": "emergency_event",
}


def register_collection(path: str, kind: str) -> None:
    async def create(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
        if kind == "tariff":
            try:
                current = store.get(kind, body.id, context.tenant_id)
            except KeyError:
                current = None
            if current and current.body.get("status") == "published":
                raise ValueError("published tariff versions are immutable; create a new tariff ID")
        return mutate(kind, body, context)

    async def listing(context: ReadContext) -> list[dict[str, Any]]:
        return list_kind(kind, context)

    create.__name__ = f"create_{kind}"
    listing.__name__ = f"list_{kind}"
    app.add_api_route(path, create, methods=["POST"], tags=[kind])
    app.add_api_route(path, listing, methods=["GET"], tags=[kind])


for collection_path, collection_kind in GENERIC_COLLECTIONS.items():
    register_collection(collection_path, collection_kind)


@app.post("/v1/admin/config")
def update_admin_config(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    return mutate("admin_config", body, context)


@app.get("/v1/admin/config")
def list_admin_config(context: ReadContext) -> list[dict[str, Any]]:
    return list_kind("admin_config", context)


@app.get("/v1/admin/config/{config_id}")
def get_admin_config(config_id: str, context: ReadContext) -> dict[str, Any]:
    return serialize(store.get("admin_config", config_id, context.tenant_id))


@app.get("/v1/admin/config/{config_id}/versions")
def admin_config_versions(config_id: str, context: ReadContext) -> list[dict[str, Any]]:
    return [serialize(item) for item in store.history("admin_config", config_id, context.tenant_id)]


@app.post("/v1/admin/config/{config_id}/rollback")
def rollback_admin_config(
    config_id: str,
    body: ConfigRollbackRequest,
    context: WriteContext,
) -> dict[str, Any]:
    if context.if_match is None:
        raise RuntimeError("If-Match is required for configuration rollback")

    def rollback() -> dict[str, Any]:
        current = store.get("admin_config", config_id, context.tenant_id)
        if current.etag != context.if_match:
            raise RuntimeError("If-Match optimistic concurrency conflict")
        target = store.get_version("admin_config", config_id, context.tenant_id, body.target_version)
        restored = store.put(
            kind="admin_config",
            resource_id=config_id,
            tenant_id=context.tenant_id,
            body={key: value for key, value in target.body.items() if key != "id"},
            idempotency_key=context.idempotency_key or "missing",
            if_match=context.if_match,
        )
        return {**serialize(restored), "rolled_back_from_version": body.target_version}

    return audited_operation(
        context,
        operation="admin_config.rollback",
        resource=config_id,
        intent={"target_version": body.target_version, "if_match": context.if_match},
        callback=rollback,
    )


def require_if_match(context: ApiContext, current: StoredResource) -> None:
    if context.if_match is None:
        raise RuntimeError("If-Match is required for this state transition")
    if context.if_match != current.etag:
        raise RuntimeError("If-Match optimistic concurrency conflict")


@app.post("/v1/tariffs/{tariff_id}/publish")
def publish_tariff(tariff_id: str, context: WriteContext) -> dict[str, Any]:
    current = store.get("tariff", tariff_id, context.tenant_id)

    def publish() -> dict[str, Any]:
        require_if_match(context, current)
        data = current.body
        TariffPlan(
            id=tariff_id,
            version=int(data.get("tariff_version", current.version)),
            currency=str(data.get("currency", "USD")),
            timezone=str(data["timezone"]),
            effective_from=date.fromisoformat(str(data["effective_from"])),
            effective_to=date.fromisoformat(str(data["effective_to"])) if data.get("effective_to") else None,
            periods=tuple(
                PricePeriod(
                    str(item["name"]),
                    time.fromisoformat(str(item["start_local"])),
                    time.fromisoformat(str(item["end_local"])),
                    Decimal(str(item["price_per_kwh"])),
                    frozenset(int(day) for day in item.get("weekdays", range(7))),
                )
                for item in data["periods"]
            ),
            demand_charge_per_kw=Decimal(str(data.get("demand_charge_per_kw", 0))),
            capacity_charge_per_kw=Decimal(str(data.get("capacity_charge_per_kw", 0))),
            tax_rate=Decimal(str(data.get("tax_rate", 0))),
            feed_in_price_per_kwh=Decimal(str(data.get("feed_in_price_per_kwh", 0))),
        )
        published = store.put(
            kind="tariff",
            resource_id=tariff_id,
            tenant_id=context.tenant_id,
            body={
                **{key: value for key, value in data.items() if key != "id"},
                "status": "published",
                "published_at": datetime.now(UTC),
            },
            idempotency_key=f"{context.idempotency_key}:tariff",
            if_match=context.if_match,
        )
        return serialize(published)

    return audited_operation(
        context,
        operation="tariff.publish",
        resource=tariff_id,
        intent={"version": current.version, "etag": current.etag},
        callback=publish,
    )


@app.post("/v1/plans")
def create_governed_plan(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    risk = RiskLevel(int(body.data.get("risk", 0)))
    data = {
        **body.data,
        "state": "draft",
        "risk": int(risk),
        "policy_versions": list(body.data.get("policy_versions", [])),
        "hard_violations": list(body.data.get("hard_violations", [])),
        "required_approvers": int(body.data.get("required_approvers", 0)),
        "created_by": context.actor_id,
    }
    if not data.get("site_id") or not data.get("state_version"):
        raise ValueError("plan requires site_id and state_version")
    return mutate("plan", ResourceRequest(id=body.id, data=data), context)


@app.get("/v1/plans")
def list_governed_plans(context: ReadContext) -> list[dict[str, Any]]:
    return list_kind("plan", context)


@app.get("/v1/plans/{plan_id}")
def get_governed_plan(plan_id: str, context: ReadContext) -> dict[str, Any]:
    return serialize(store.get("plan", plan_id, context.tenant_id))


@app.post("/v1/plans/{plan_id}/validate")
def validate_governed_plan(plan_id: str, context: WriteContext) -> dict[str, Any]:
    current = store.get("plan", plan_id, context.tenant_id)

    def validate() -> dict[str, Any]:
        require_if_match(context, current)
        if current.body.get("state") != "draft":
            raise ValueError("only a draft plan can be validated")
        if current.body.get("hard_violations"):
            raise ValueError("plan with hard violations cannot be validated")
        if not current.body.get("policy_versions"):
            raise ValueError("plan validation requires exact policy versions")
        updated = store.put(
            kind="plan",
            resource_id=plan_id,
            tenant_id=context.tenant_id,
            body={
                **{key: value for key, value in current.body.items() if key != "id"},
                "state": "validated",
                "validated_at": datetime.now(UTC),
                "validated_by": context.actor_id,
            },
            idempotency_key=f"{context.idempotency_key}:plan",
            if_match=context.if_match,
        )
        return serialize(updated)

    return audited_operation(
        context,
        operation="plan.validate",
        resource=plan_id,
        intent={"version": current.version, "etag": current.etag},
        callback=validate,
    )


@app.post("/v1/plans/{plan_id}/compare")
def compare_governed_plans(plan_id: str, body: PlanCompareRequest, context: WriteContext) -> dict[str, Any]:
    baseline = store.get("plan", plan_id, context.tenant_id)
    candidate = store.get("plan", body.candidate_id, context.tenant_id)

    def compare() -> dict[str, Any]:
        if "schedule" not in baseline.body or "schedule" not in candidate.body:
            raise ValueError("both plans require a schedule for comparison")
        difference = compare_plans(
            parse_schedule_plan(dict(baseline.body["schedule"])),
            parse_schedule_plan(dict(candidate.body["schedule"])),
        )
        return {
            **asdict(difference),
            "baseline_id": plan_id,
            "baseline_version": baseline.version,
            "candidate_id": body.candidate_id,
            "candidate_version": candidate.version,
        }

    return audited_operation(
        context,
        operation="plan.compare",
        resource=plan_id,
        intent={
            "baseline_version": baseline.version,
            "candidate_id": body.candidate_id,
            "candidate_version": candidate.version,
        },
        callback=compare,
    )


@app.post("/v1/plans/{plan_id}/submit-approval")
def submit_plan_approval(
    plan_id: str,
    body: ApprovalSubmissionRequest,
    context: WriteContext,
) -> dict[str, Any]:
    if body.expires_at <= datetime.now(UTC):
        raise ValueError("approval expiry must be in the future")
    current = store.get("plan", plan_id, context.tenant_id)
    risk = RiskLevel(int(current.body["risk"]))
    approval_id = f"approval-{plan_id}-{current.version + 1}"

    def submit() -> dict[str, Any]:
        require_if_match(context, current)
        if current.body.get("state") != "validated":
            raise ValueError("only a validated plan can enter approval")
        target_state = "pending_approval" if risk >= RiskLevel.L2 else "approved"
        updated = store.put(
            kind="plan",
            resource_id=plan_id,
            tenant_id=context.tenant_id,
            body={
                **{key: value for key, value in current.body.items() if key != "id"},
                "state": target_state,
                "approval_id": approval_id if risk >= RiskLevel.L2 else None,
            },
            idempotency_key=f"{context.idempotency_key}:plan",
            if_match=context.if_match,
        )
        approval: dict[str, Any] | None = None
        if risk >= RiskLevel.L2:
            request = ApprovalRequest(
                approval_id,
                plan_id,
                context.tenant_id,
                risk,
                context.actor_id,
                body.expires_at,
                body.required_roles,
                body.required_count,
            )
            approval_workflow.create(request)
            approval = asdict(request)
        return {"plan": serialize(updated), "approval": approval}

    return audited_operation(
        context,
        operation="plan.submit_approval",
        resource=plan_id,
        intent={"version": current.version, **body.model_dump(mode="json")},
        callback=submit,
    )


@app.post("/v1/approvals/{approval_id}/modify")
def modify_approval(
    approval_id: str,
    body: ApprovalSubmissionRequest,
    context: WriteContext,
) -> dict[str, Any]:
    if body.expires_at <= datetime.now(UTC):
        raise ValueError("approval expiry must be in the future")

    def modify() -> dict[str, Any]:
        return asdict(
            approval_workflow.modify(
                approval_id,
                actor_id=context.actor_id,
                expires_at=body.expires_at,
                required_roles=body.required_roles,
                required_count=body.required_count,
                tenant_id=context.tenant_id,
            )
        )

    return audited_operation(
        context,
        operation="approval.modify",
        resource=approval_id,
        intent=body.model_dump(mode="json"),
        callback=modify,
    )


@app.post("/v1/recovery/plan")
def recovery_plan(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    def create() -> dict[str, Any]:
        loads = tuple(
            CriticalLoad(
                id=str(item["id"]),
                power_kw=Decimal(str(item["power_kw"])),
                priority=int(item["priority"]),
                critical=bool(item["critical"]),
            )
            for item in body.data["loads"]
        )
        return asdict(plan_degraded_mode(loads, Decimal(str(body.data["available_power_kw"]))))

    return audited_operation(
        context,
        operation="recovery.plan",
        resource=body.id,
        intent=body.data,
        callback=create,
    )


@app.post("/v1/connectors/{connector_id}/test")
def test_compute_connector(connector_id: str, context: WriteContext) -> dict[str, Any]:
    configuration = store.get("connector", connector_id, context.tenant_id).body

    def probe() -> dict[str, Any]:
        if configuration.get("type") == "https_meter":
            meter_adapter = create_meter_connector(connector_id, configuration)
            valid = meter_adapter.probe()
            adapter_name = type(meter_adapter).__name__
            read_only = True
        else:
            compute_adapter = create_compute_adapter(connector_id, configuration, tenant_id=context.tenant_id)
            valid = compute_adapter.validate_credentials()
            adapter_name = type(compute_adapter).__name__
            read_only = compute_adapter.read_only
        return {
            "connector_id": connector_id,
            "adapter": adapter_name,
            "read_only": read_only,
            "credentials_valid": valid,
            "external_probe": "PASS" if valid else "FAIL",
        }

    return audited_operation(
        context,
        operation="connector.test",
        resource=connector_id,
        intent={"connector_version": store.get("connector", connector_id, context.tenant_id).version},
        callback=probe,
    )


@app.post("/v1/connectors/compute/{connector_id}/sync")
def sync_compute_connector(connector_id: str, context: WriteContext) -> dict[str, Any]:
    configuration = store.get("connector", connector_id, context.tenant_id)

    def synchronize() -> dict[str, Any]:
        adapter = create_compute_adapter(connector_id, configuration.body, tenant_id=context.tenant_id)
        if not adapter.validate_credentials():
            raise ValueError("compute connector credential validation failed")
        topology_version = int(configuration.body.get("topology_version", 1))
        if topology_version < 1:
            raise ValueError("compute connector topology_version must be positive")
        snapshot = adapter.snapshot(context.tenant_id, topology_version)
        try:
            previous = store.get("compute_snapshot", connector_id, context.tenant_id)
            if_match = previous.etag
        except KeyError:
            if_match = None
        resource = store.put(
            kind="compute_snapshot",
            resource_id=connector_id,
            tenant_id=context.tenant_id,
            body=asdict(snapshot),
            idempotency_key=f"{context.idempotency_key}:snapshot",
            if_match=if_match,
        )
        return serialize(resource)

    return audited_operation(
        context,
        operation="compute_connector.sync",
        resource=connector_id,
        intent={"connector_version": configuration.version, "connector_etag": configuration.etag},
        callback=synchronize,
    )


@app.put("/v1/quotas/current")
def configure_quota(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    if not context.roles.intersection({"admin", "tenant_admin"}):
        raise PermissionError("quota changes require tenant admin role")
    quota = Quota(
        int(body.data["max_gpus"]),
        Decimal(str(body.data["max_gpu_hours"])),
        int(body.data["max_concurrent_jobs"]),
    )

    def configure() -> dict[str, Any]:
        quota_ledger.configure(context.tenant_id, quota)
        return {**asdict(quota), "usage": quota_ledger.usage(context.tenant_id)}

    return audited_operation(
        context,
        operation="quota.configure",
        resource=context.tenant_id,
        intent=body.data,
        callback=configure,
    )


@app.get("/v1/quotas/current")
def quota_usage(context: ReadContext) -> dict[str, Any]:
    used_gpus, used_gpu_hours, active_jobs = quota_ledger.usage(context.tenant_id)
    return {
        "used_gpus": used_gpus,
        "used_gpu_hours": used_gpu_hours,
        "active_jobs": active_jobs,
    }


@app.post("/v1/admission/evaluate")
def evaluate_admission(body: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    job = parse_job(body["job"], context.tenant_id)
    snapshot_data = body["snapshot"]
    nodes = tuple(
        ComputeNode(
            id=str(item["id"]),
            tenant_id=context.tenant_id,
            site_id=str(item["site_id"]),
            topology_asset_id=str(item["topology_asset_id"]),
            gpus=tuple(
                Gpu(
                    str(gpu["id"]),
                    str(gpu["model"]),
                    Decimal(str(gpu["memory_gb"])),
                    Decimal(str(gpu["max_power_kw"])),
                )
                for gpu in item["gpus"]
            ),
            cpu_cores=int(item.get("cpu_cores", 0)),
            memory_gb=Decimal(str(item.get("memory_gb", 0))),
        )
        for item in snapshot_data["nodes"]
    )
    snapshot = SnapshotBuilder().build(
        tenant_id=context.tenant_id,
        topology_version=int(snapshot_data["topology_version"]),
        source=str(snapshot_data.get("source", "api")),
        nodes=nodes,
        observed_at=datetime.fromisoformat(str(snapshot_data["observed_at"])),
        now=datetime.fromisoformat(str(body["now"])),
    )
    if runtime is None:
        quota_data = body["quota"]
        quota_ledger.configure(
            context.tenant_id,
            Quota(
                int(quota_data["max_gpus"]),
                Decimal(str(quota_data["max_gpu_hours"])),
                int(quota_data["max_concurrent_jobs"]),
            ),
        )
    result = AdmissionService(quota_ledger).evaluate(
        job,
        snapshot,
        datetime.fromisoformat(str(body["now"])),
    )
    return asdict(result)


@app.post("/v1/schedules/baseline/{strategy}")
def baseline_schedule(strategy: str, body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    request = parse_schedule_input(body.data, context.tenant_id)
    strategies = {
        "fifo": schedule_fifo,
        "priority_edf": schedule_priority_edf,
        "price_aware": schedule_price_aware,
    }
    try:
        plan = strategies[strategy](request)
    except KeyError as error:
        raise ValueError(f"unknown baseline strategy {strategy}") from error
    return asdict(plan)


@app.post("/v1/benchmarks")
def create_benchmark(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    result = benchmark(parse_schedule_input(body.data, context.tenant_id), int(body.data.get("seed", 0)))
    return mutate("benchmark", ResourceRequest(id=body.id, data=asdict(result)), context)


@app.post("/v1/optimization-runs")
def create_optimization_run(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    request = parse_schedule_input(body.data, context.tenant_id)
    result = HighsSolver().solve(
        request,
        timeout_seconds=float(body.data.get("timeout_seconds", 10)),
    )
    stored = mutate("optimization_run", ResourceRequest(id=body.id, data=asdict(result)), context)
    explanation = explain_plan(
        result,
        input_hash=request.content_hash(),
        model_version=request.forecast_version,
        forecast_quality=Decimal(str(body.data.get("forecast_quality", 1))),
    )
    store.put(
        kind="explanation",
        resource_id=body.id,
        tenant_id=context.tenant_id,
        body=asdict(explanation),
        idempotency_key=f"{context.idempotency_key}:explanation",
    )
    return stored


@app.post("/v1/mpc/{controller_id}/cycle")
def create_mpc_cycle(controller_id: str, body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    observed_data = body.data["observed"]
    observed = ObservedState(
        completed_jobs=frozenset(str(item) for item in observed_data.get("completed_jobs", [])),
        running_progress={
            str(key): Decimal(str(value)) for key, value in dict(observed_data.get("running_progress", {})).items()
        },
        available_gpus=int(observed_data["available_gpus"]),
        battery_soc=Decimal(str(observed_data["battery_soc"])),
        grid_limit_kw=Decimal(str(observed_data["grid_limit_kw"])),
        data_quality=Decimal(str(observed_data["data_quality"])),
        version=str(observed_data["version"]),
    )

    def run_cycle() -> dict[str, Any]:
        cycle = mpc_repository.cycle(
            controller_id,
            context.tenant_id,
            parse_schedule_input(body.data["schedule"], context.tenant_id),
            observed,
            started_at=datetime.fromisoformat(str(body.data["started_at"])),
            timeout_seconds=float(body.data.get("timeout_seconds", 5)),
            idempotency_key=context.idempotency_key or "missing-idempotency-key",
        )
        return asdict(cycle)

    return audited_operation(
        context,
        operation="mpc.cycle",
        resource=controller_id,
        intent=body.data,
        callback=run_cycle,
    )


def parse_policy(body: ResourceRequest, tenant_id: str) -> Policy:
    data = body.data
    return Policy(
        body.id,
        int(data["version"]),
        tenant_id,
        frozenset(str(item) for item in data.get("site_ids", [])),
        PolicyRule(
            str(data["rule"]["field"]),
            str(data["rule"]["operator"]),
            data["rule"]["value"],
        ),
        Enforcement(data["enforcement"]),
        int(data["priority"]),
        str(data["owner"]),
    )


@app.post("/v1/policies/validate")
def validate_policy(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    policy = parse_policy(body, context.tenant_id)
    policy_engine.validate_publish(policy)
    return {"valid": True, "policy": asdict(policy)}


@app.post("/v1/policies/publish")
def publish_policy(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    policy = parse_policy(body, context.tenant_id)
    return audited_operation(
        context,
        operation="policy.publish",
        resource=f"{policy.id}@{policy.version}",
        intent=body.data,
        callback=lambda: asdict(policy_engine.publish(policy, context.roles)),
    )


@app.post("/v1/policies/dry-run")
def dry_run_policy(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    data = body.data
    return asdict(policy_engine.evaluate(context.tenant_id, str(data["site_id"]), dict(data["facts"])))


@app.post("/v1/approvals")
def create_approval(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    data = body.data
    request = ApprovalRequest(
        id=body.id,
        plan_id=str(data["plan_id"]),
        tenant_id=context.tenant_id,
        risk=RiskLevel(int(data["risk"])),
        requested_by=context.actor_id,
        expires_at=datetime.fromisoformat(str(data["expires_at"])),
        required_roles=frozenset(str(item) for item in data["required_roles"]),
        required_count=int(data["required_count"]),
    )

    def create() -> dict[str, Any]:
        approval_workflow.create(request)
        return asdict(request)

    return audited_operation(
        context,
        operation="approval.create",
        resource=request.id,
        intent=body.data,
        callback=create,
    )


@app.get("/v1/approvals")
def list_approvals(context: ReadContext) -> list[dict[str, Any]]:
    return [asdict(item) for item in approval_workflow.list(context.tenant_id)]


@app.post("/v1/approvals/{approval_id}/approve")
def approve_request(approval_id: str, body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    requested_role = str(body.data["role"])
    if requested_role not in context.roles:
        raise PermissionError("approval role is not assigned to authenticated identity")

    def approve() -> dict[str, Any]:
        approval = approval_workflow.approve(
            approval_id,
            actor_id=context.actor_id,
            role=requested_role,
            now=datetime.fromisoformat(str(body.data["now"])),
            tenant_id=context.tenant_id,
        )
        plan_result: dict[str, Any] | None = None
        if approval.status == ApprovalStatus.APPROVED:
            try:
                plan = store.get("plan", approval.plan_id, context.tenant_id)
            except KeyError:
                plan = None
            if plan and plan.body.get("state") == "pending_approval":
                updated = store.put(
                    kind="plan",
                    resource_id=approval.plan_id,
                    tenant_id=context.tenant_id,
                    body={
                        **{key: value for key, value in plan.body.items() if key != "id"},
                        "state": "approved",
                        "approved_at": datetime.fromisoformat(str(body.data["now"])),
                    },
                    idempotency_key=f"{context.idempotency_key}:plan",
                    if_match=plan.etag,
                )
                plan_result = serialize(updated)
        return {**asdict(approval), "plan": plan_result}

    return audited_operation(
        context,
        operation="approval.approve",
        resource=approval_id,
        intent=body.data,
        callback=approve,
    )


@app.post("/v1/approvals/{approval_id}/reject")
def reject_request(approval_id: str, body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    def reject() -> dict[str, Any]:
        request = approval_workflow.get(approval_id, tenant_id=context.tenant_id)
        if request.tenant_id != context.tenant_id:
            raise PermissionError("cross-tenant approval access")
        rejected = approval_workflow.reject(approval_id, tenant_id=context.tenant_id)
        plan_result: dict[str, Any] | None = None
        try:
            plan = store.get("plan", rejected.plan_id, context.tenant_id)
        except KeyError:
            plan = None
        if plan and plan.body.get("state") == "pending_approval":
            updated = store.put(
                kind="plan",
                resource_id=rejected.plan_id,
                tenant_id=context.tenant_id,
                body={
                    **{key: value for key, value in plan.body.items() if key != "id"},
                    "state": "rejected",
                    "rejected_at": datetime.now(UTC),
                },
                idempotency_key=f"{context.idempotency_key}:plan",
                if_match=plan.etag,
            )
            plan_result = serialize(updated)
        return {**asdict(rejected), "plan": plan_result}

    return audited_operation(
        context,
        operation="approval.reject",
        resource=approval_id,
        intent=body.data,
        callback=reject,
    )


def parse_action(action_id: str, data: dict[str, Any], tenant_id: str) -> Action:
    return Action(
        action_id,
        str(data["plan_id"]),
        tenant_id,
        str(data["target"]),
        str(data["kind"]),
        str(data["expected_state_version"]),
        dict(data["parameters"]),
        {
            str(name): (Decimal(str(bounds[0])), Decimal(str(bounds[1])))
            for name, bounds in dict(data.get("bounds", {})).items()
        },
        int(data.get("timeout_seconds", 30)),
        str(data["idempotency_key"]),
        RiskLevel(int(data["risk"])),
        datetime.fromisoformat(str(data["created_at"])),
        data.get("compensation_kind"),
    )


def dry_run_executor(action: Action) -> dict[str, Any]:
    if action.target == "simulator":
        if settings.environment not in {"test", "simulator"}:
            raise PermissionError("simulator actions cannot be used by a production API")
        return simulator_executor.dry_run(action.kind, action.parameters)
    return guarded_executor.dry_run(action.target, action.kind, action.parameters)


def execute_with_adapter(action: Action) -> dict[str, Any]:
    if action.target == "simulator":
        if settings.environment not in {"test", "simulator"}:
            raise PermissionError("simulator actions cannot be executed by a production API")
        return simulator_executor.execute(action.kind, action.parameters)
    return guarded_executor.execute(
        action.target,
        action.kind,
        action.parameters,
        idempotency_key=action.idempotency_key,
    )


@app.post("/v1/actions/{action_id}/cancel")
def cancel_action(action_id: str, body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    if body.id != action_id:
        raise ValueError("action ID mismatch")
    reason = str(body.data.get("reason", "")).strip()
    return audited_operation(
        context,
        operation="action.cancel",
        resource=action_id,
        intent={"reason": reason},
        callback=lambda: execution_idempotency.cancel(
            action_id,
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            reason=reason,
        ),
    )


@app.post("/v1/actions/{action_id}/dry-run")
def dry_run_action(action_id: str, body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    action = parse_action(action_id, body.data, context.tenant_id)
    approval = (
        approval_workflow.get(str(body.data["approval_id"]), tenant_id=context.tenant_id)
        if body.data.get("approval_id")
        else None
    )
    decision = ActionGuard(frozenset({"schedule_job", "set_dispatch_plan"})).evaluate(
        action,
        current_state_version=str(body.data["current_state_version"]),
        now=datetime.fromisoformat(str(body.data["now"])),
        approval=approval,
    )
    return {"guard": asdict(decision), "adapter": dry_run_executor(action)}


@app.post("/v1/actions/{action_id}/execute")
def execute_action(action_id: str, body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    action = parse_action(action_id, body.data, context.tenant_id)
    approval = (
        approval_workflow.get(str(body.data["approval_id"]), tenant_id=context.tenant_id)
        if body.data.get("approval_id")
        else None
    )
    decision = ActionGuard(frozenset({"schedule_job", "set_dispatch_plan"})).evaluate(
        action,
        current_state_version=str(body.data["current_state_version"]),
        now=datetime.fromisoformat(str(body.data["now"])),
        approval=approval,
    )
    if not decision.allowed:
        raise PermissionError(f"Action Guard rejected: {decision.reasons}")
    return execution_idempotency.execute_once(
        action.idempotency_key,
        {"action": action.id, "parameters": action.parameters},
        lambda: execute_with_adapter(action),
        tenant_id=context.tenant_id,
        action_id=action.id,
    )


@app.post("/v1/migrations/evaluate")
def migration_evaluation(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    data = body.data
    job = parse_job(data["job"], context.tenant_id)

    def site(item: dict[str, Any]) -> Site:
        return Site(
            id=str(item["id"]),
            region=str(item["region"]),
            available_gpus=int(item["available_gpus"]),
            grid_limit_kw=Decimal(str(item["grid_limit_kw"])),
            energy_price=Decimal(str(item["energy_price"])),
            carbon_intensity=Decimal(str(item["carbon_intensity"])),
            online=bool(item.get("online", True)),
        )

    source = site(data["source"])
    destination = site(data["destination"])
    link_data = data["link"]
    link = SiteLink(
        source=str(link_data["source"]),
        destination=str(link_data["destination"]),
        bandwidth_gbps=Decimal(str(link_data["bandwidth_gbps"])),
        latency_ms=Decimal(str(link_data["latency_ms"])),
        transfer_cost_per_gb=Decimal(str(link_data["transfer_cost_per_gb"])),
        online=bool(link_data.get("online", True)),
    )
    return asdict(
        evaluate_migration(
            job,
            source,
            destination,
            link,
            checkpoint_size_gb=Decimal(str(data["checkpoint_size_gb"])),
            remaining_hours=Decimal(str(data["remaining_hours"])),
        )
    )


@app.post("/v1/multisite/optimize")
def multisite_optimize(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    return migration_evaluation(body, context)


@app.post("/v1/island/plan")
def island_plan(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    del context
    data = body.data
    loads = tuple(
        CriticalLoad(
            str(item["id"]),
            Decimal(str(item["power_kw"])),
            int(item["priority"]),
            bool(item["critical"]),
        )
        for item in data["loads"]
    )
    return asdict(
        plan_island_survival(
            loads,
            battery_kwh=Decimal(str(data["battery_kwh"])),
            reserved_kwh=Decimal(str(data["reserved_kwh"])),
            generator_kwh=Decimal(str(data.get("generator_kwh", 0))),
            pv_kw=Decimal(str(data.get("pv_kw", 0))),
        )
    )


@app.post("/v1/replays")
def create_replay(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    result = replay(list(body.data["events"]))
    return mutate("evaluation", ResourceRequest(id=body.id, data=asdict(result)), context)


@app.post("/v1/what-if")
def create_what_if(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    result = run_what_if(
        parse_schedule_input(body.data["schedule"], context.tenant_id),
        capacity_multiplier=Decimal(str(body.data.get("capacity_multiplier", 1))),
    )
    return mutate("what_if", ResourceRequest(id=body.id, data=asdict(result)), context)


@app.get("/v1/compute/nodes")
def compute_nodes(context: ReadContext) -> list[dict[str, Any]]:
    return [
        dict(node)
        for snapshot in store.list("compute_snapshot", context.tenant_id)
        for node in snapshot.body.get("nodes", [])
        if isinstance(node, dict)
    ]


@app.get("/v1/compute/gpus")
def compute_gpus(context: ReadContext) -> list[dict[str, Any]]:
    return [dict(gpu) for node in compute_nodes(context) for gpu in node.get("gpus", []) if isinstance(gpu, dict)]


@app.get("/v1/compute/reservations")
def compute_reservations(context: ReadContext) -> list[dict[str, Any]]:
    return [
        dict(reservation)
        for snapshot in store.list("compute_snapshot", context.tenant_id)
        for reservation in snapshot.body.get("reservations", [])
        if isinstance(reservation, dict)
    ]


@app.get("/v1/compute/snapshots/{resource_id}")
def compute_snapshot(resource_id: str, context: ReadContext) -> dict[str, Any]:
    return serialize(store.get("compute_snapshot", resource_id, context.tenant_id))


@app.get("/v1/jobs/{resource_id}")
def job(resource_id: str, context: ReadContext) -> dict[str, Any]:
    return serialize(store.get("job", resource_id, context.tenant_id))


@app.post("/v1/jobs/{resource_id}/cancel")
def cancel_job(resource_id: str, body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    return mutate("job_cancel", body.model_copy(update={"id": resource_id}), context)


@app.get("/v1/energy/state")
def energy_state(context: ReadContext) -> list[dict[str, Any]]:
    return list_kind("energy_state", context)


@app.get("/v1/data-quality/status")
def quality_status(context: ReadContext) -> dict[str, Any]:
    points = sum(
        (
            list(
                timeseries.query(
                    context.tenant_id,
                    metric,
                    datetime.min.replace(tzinfo=UTC),
                    datetime.max.replace(tzinfo=UTC),
                )
            )
            for metric in {
                point.metric for raw in raw_landing.query(context.tenant_id) for point in [Normalizer().normalize(raw)]
            }
        ),
        [],
    )
    return {"status": "good" if points else "unknown", "point_count": len(points)}


@app.get("/v1/forecasts/{resource_id}")
def forecast(resource_id: str, context: ReadContext) -> dict[str, Any]:
    return serialize(store.get("forecast", resource_id, context.tenant_id))


@app.post("/v1/forecasts/generate")
def generate_forecast(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    data = body.data
    history = tuple(
        ObservedValue(datetime.fromisoformat(str(item["timestamp"])), Decimal(str(item["value"])))
        for item in data["history"]
    )
    bundle = PersistenceModel().forecast(
        history,
        start=datetime.fromisoformat(str(data["start"])),
        periods=int(data["periods"]),
        step=timedelta(minutes=int(data.get("step_minutes", 60))),
        model_version=str(data.get("model_version", "persistence-1")),
        signal=str(data["signal"]),
    )
    return mutate("forecast", ResourceRequest(id=body.id, data=asdict(bundle)), context)


@app.post("/v1/models/register")
def register_model(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    data = body.data
    model = ModelVersion(
        name=body.id,
        version=str(data["version"]),
        artifact_hash=str(data["artifact_hash"]),
        dataset_hash=str(data["dataset_hash"]),
        created_at=datetime.fromisoformat(str(data["created_at"])),
    )

    def register() -> dict[str, Any]:
        model_registry.register(model, tenant_id=context.tenant_id)
        return asdict(model)

    return audited_operation(
        context,
        operation="model.register",
        resource=f"{model.name}@{model.version}",
        intent=body.data,
        callback=register,
    )


@app.post("/v1/models/{model_name}/promote")
def promote_model(model_name: str, body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    if not context.roles.intersection({"admin", "model_admin", "safety_admin"}):
        raise PermissionError("model promotion requires admin role")
    return audited_operation(
        context,
        operation="model.promote",
        resource=f"{model_name}@{body.data['version']}",
        intent=body.data,
        callback=lambda: asdict(
            model_registry.promote(model_name, str(body.data["version"]), tenant_id=context.tenant_id)
        ),
    )


@app.post("/v1/models/{model_name}/rollback")
def rollback_model(model_name: str, body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    if not context.roles.intersection({"admin", "model_admin", "safety_admin"}):
        raise PermissionError("model rollback requires admin role")
    return audited_operation(
        context,
        operation="model.rollback",
        resource=model_name,
        intent=body.data,
        callback=lambda: asdict(model_registry.rollback(model_name, tenant_id=context.tenant_id)),
    )


@app.get("/v1/benchmarks/{resource_id}")
def benchmark_result(resource_id: str, context: ReadContext) -> dict[str, Any]:
    return serialize(store.get("benchmark", resource_id, context.tenant_id))


@app.get("/v1/optimization-runs/{resource_id}")
def optimization_result(resource_id: str, context: ReadContext) -> dict[str, Any]:
    return serialize(store.get("optimization_run", resource_id, context.tenant_id))


@app.get("/v1/mpc/{resource_id}/cycles/{cycle_id}")
def mpc_cycle(resource_id: str, cycle_id: str, context: ReadContext) -> dict[str, Any]:
    return serialize(store.get("mpc_cycle", f"{resource_id}-{cycle_id}", context.tenant_id))


@app.get("/v1/plans/{resource_id}/explanation")
def plan_explanation(resource_id: str, context: ReadContext) -> dict[str, Any]:
    return serialize(store.get("explanation", resource_id, context.tenant_id))


@app.get("/v1/reports/{resource_id}")
def report(resource_id: str, context: ReadContext) -> dict[str, Any]:
    return serialize(store.get("report", resource_id, context.tenant_id))


@app.post("/v1/reports")
def create_report(body: ResourceRequest, context: WriteContext) -> dict[str, Any]:
    request = parse_schedule_input(body.data["schedule"], context.tenant_id)
    baseline = schedule_fifo(request)
    optimized = HighsSolver().solve(request)
    if optimized.plan is None:
        raise ValueError(f"cannot report an infeasible plan: {optimized.diagnostics}")
    report_data = build_savings_report(
        baseline,
        optimized.plan,
        tariff_version=str(body.data["tariff_version"]),
        run_id=body.id,
        uncertainty=tuple(str(item) for item in body.data.get("uncertainty", [])),
    )
    return mutate("report", ResourceRequest(id=body.id, data=asdict(report_data)), context)


@app.get("/v1/chargeback")
def chargeback(context: ReadContext) -> list[dict[str, Any]]:
    return list_kind("chargeback", context)


@app.get("/v1/evaluations/{resource_id}")
def evaluation(resource_id: str, context: ReadContext) -> dict[str, Any]:
    return serialize(store.get("evaluation", resource_id, context.tenant_id))


@app.get("/v1/certification/{release_id}")
def certification(release_id: str, context: ReadContext) -> dict[str, Any]:
    del context
    try:
        return certification_repository.view(release_id)
    except (FileNotFoundError, ValueError):
        if settings.environment == "production":
            raise KeyError(f"certification release not found or invalid: {release_id}") from None
    now = datetime.now(UTC)
    result = certify_release(
        release_id=release_id,
        commit="UNVERSIONED",
        generated_at=now,
        gate_results=(
            GateResult("build", False, (), "current checkout has no Git commit evidence"),
            GateResult("tests", False, (), "invoke make evidence"),
            GateResult("contracts", True, ("/openapi.json",)),
            GateResult("scenarios", True, ("deterministic simulator",)),
            GateResult("security", False, (), "external security gate not run"),
            GateResult("performance", False, (), "production load gate not run"),
            GateResult("backup_restore", False, (), "restore rehearsal not run"),
            GateResult("external_integrations", False, (), "external connectors are read-only"),
            GateResult("acceptance", False, (), "signed acceptance unavailable"),
        ),
    )
    return json.loads(json.dumps(asdict(result), default=str))


@app.post("/v1/certification/{release_id}/revoke")
def revoke_certification(
    release_id: str,
    body: CertificationRevocationRequest,
    context: WriteContext,
) -> dict[str, Any]:
    if "admin" not in context.roles:
        raise PermissionError("release revocation requires admin role")
    return audited_operation(
        context,
        operation="certification.revoke",
        resource=release_id,
        intent={"release_id": release_id, "reason": body.reason},
        callback=lambda: certification_repository.revoke(
            release_id,
            actor_id=context.actor_id,
            reason=body.reason,
        ),
    )
