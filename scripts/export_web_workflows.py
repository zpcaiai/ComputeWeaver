from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = ROOT / "schemas" / "openapi" / "openapi.json"
OUTPUT_PATH = ROOT / "apps" / "web" / "src" / "workflows.generated.json"
HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})

SKILLS: tuple[tuple[str, str, str], ...] = (
    ("B01", "Platform foundation", "Runtime health, durable jobs, audit integrity and reproducible service contracts."),
    ("B02", "Unified contracts", "Canonical API, event, unit and schema contracts shared by every workflow."),
    ("B03", "Asset topology", "Create, inspect, validate and publish immutable topology versions."),
    ("B04", "Compute resource plane", "Inspect GPU capacity and synchronize governed scheduler inventory."),
    ("B05", "Workload and admission", "Submit workloads, evaluate admission, reserve capacity and govern quotas."),
    ("B06", "Tariff and carbon", "Manage tariff versions and calculate explainable energy cost outcomes."),
    ("B07", "Energy assets", "Inspect energy state and validate physically bounded power dispatch."),
    ("B08", "Data ingestion", "Operate connectors, time-series queries and data-quality recovery."),
    ("B09", "Shadow simulator", "Create and control deterministic, production-isolated simulations."),
    ("B10", "Scenario evaluation", "Validate scenarios, replay evidence and compare disturbance outcomes."),
    ("B11", "Forecasting center", "Generate uncertainty-aware forecasts and govern model lifecycle."),
    ("B12", "Baseline schedulers", "Run deterministic baselines and comparable benchmark episodes."),
    ("B13", "MILP optimizer", "Create content-bound co-optimization runs and inspect diagnostics."),
    ("B14", "Rolling MPC", "Create controllers, execute bounded cycles and inspect fallback state."),
    ("B15", "Plan governance", "Validate policies and govern plan comparison and approval transitions."),
    ("B16", "Guarded execution", "Approve, dry-run, execute, cancel and compensate bounded actions."),
    ("B17", "Explainability", "Inspect reasons, run isolated what-if analysis and publish reports."),
    ("B18", "Enterprise platform", "Operate tenant IAM, budgets, chargeback, notifications and administration."),
    ("B19", "Multi-site resilience", "Evaluate migrations, emergency, island and recovery plans."),
    (
        "B20",
        "Production certification",
        "Request external evidence, run gates, publish signed releases and revoke safely.",
    ),
)


def _batch_for(method: str, path: str) -> str:
    del method
    ordered: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("B20", ("/v1/certification/", "/v1/artifacts/")),
        ("B19", ("/v1/multisite/", "/v1/migrations/", "/v1/emergency/", "/v1/island/", "/v1/recovery/")),
        (
            "B18",
            (
                "/v1/tenants",
                "/v1/users",
                "/v1/roles",
                "/v1/budgets",
                "/v1/chargeback",
                "/v1/notifications/",
                "/v1/admin/",
            ),
        ),
        ("B17", ("/v1/what-if", "/v1/reports", "/v1/plans/{resource_id}/explanation")),
        ("B16", ("/v1/approvals", "/v1/actions/")),
        ("B15", ("/v1/policies/", "/v1/plans")),
        ("B14", ("/v1/mpc",)),
        ("B13", ("/v1/optimization-runs",)),
        ("B12", ("/v1/schedules/", "/v1/benchmarks")),
        ("B11", ("/v1/forecasts", "/v1/models/")),
        ("B10", ("/v1/scenarios/", "/v1/scenario-runs", "/v1/replays", "/v1/evaluations/")),
        ("B09", ("/v1/simulations",)),
        ("B08", ("/v1/connectors", "/v1/timeseries/", "/v1/data-quality/")),
        ("B07", ("/v1/energy/",)),
        ("B06", ("/v1/tariffs", "/v1/cost/")),
        ("B05", ("/v1/jobs", "/v1/admission/", "/v1/reservations", "/v1/quotas/")),
        ("B04", ("/v1/compute/",)),
        ("B03", ("/v1/assets", "/v1/topology/")),
        ("B01", ("/health/", "/version", "/v1/system/", "/v1/audit/")),
    )
    for batch, prefixes in ordered:
        if any(path.startswith(prefix) for prefix in prefixes):
            if path.startswith("/v1/connectors/compute/"):
                return "B04"
            return batch
    raise ValueError(f"front-end workflow is not assigned to a skill: {path}")


def _risk(method: str, path: str) -> str:
    if method == "GET":
        return "read"
    high_markers = (
        "/execute",
        "/publish",
        "/revoke",
        "/approve",
        "/reject",
        "/rollback",
        "/promote",
        "/cancel",
        "/emergency/",
        "/island/",
    )
    if any(marker in path for marker in high_markers):
        return "high"
    controlled_markers = ("/validate", "/evaluate", "/compare", "/dry-run", "/test", "/external-readiness")
    return "controlled" if any(marker in path for marker in controlled_markers) else "standard"


def _presentation(path: str) -> str:
    if path.startswith("/v1/certification/"):
        return "certification"
    if path.endswith("/graph"):
        return "graph"
    if any(marker in path for marker in ("/cycles/", "/events", "/audit/records")):
        return "timeline"
    if any(marker in path for marker in ("/evaluations/", "/benchmarks/", "/chargeback", "/data-quality/")):
        return "metrics"
    return "table"


def _parameter_default(name: str, schema: dict[str, Any]) -> str:
    if "default" in schema:
        return str(schema["default"])
    values = {
        "action_id": "action-001",
        "approval_id": "approval-001",
        "artifact_key": "releases/evidence.json",
        "asset_id": "asset-001",
        "config_id": "runtime",
        "connector_id": "connector-001",
        "controller_id": "mpc-001",
        "cycle_id": "cycle-001",
        "job_id": "1",
        "model_name": "workload-runtime",
        "operation": "start",
        "plan_id": "plan-001",
        "release_id": "$release_id",
        "resource_id": "resource-001",
        "simulation_id": "simulation-001",
        "strategy": "fifo",
        "tariff_id": "tariff-001",
        "version": "1",
        "metric": "facility_power_kw",
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-02T00:00:00Z",
    }
    return values.get(name, "1" if schema.get("type") == "integer" else name.replace("_", "-"))


def _example(schema: dict[str, Any], components: dict[str, Any], *, name: str = "value") -> Any:
    if "$ref" in schema:
        resolved = components
        for part in str(schema["$ref"]).split("/")[2:]:
            resolved = resolved[part]
        return _example(resolved, components, name=name)
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][0]
    schema_type = schema.get("type")
    if schema_type == "object" or "properties" in schema:
        properties = schema.get("properties", {})
        return {key: _example(value, components, name=key) for key, value in properties.items()}
    if schema_type == "array":
        return []
    if schema_type == "boolean":
        return False
    if schema_type in {"integer", "number"}:
        return schema.get("minimum", 1)
    if schema.get("format") == "date-time":
        return "2026-01-01T00:00:00Z"
    if name == "expected_source_revision":
        return "$source_revision"
    if name == "expected_certificate_hash":
        return "$certificate_hash"
    if name == "reason":
        return "Operational revocation requested after verified incident review"
    return name.replace("_", "-")


def _body_example(method: str, path: str, operation: dict[str, Any], components: dict[str, Any]) -> Any | None:
    content = operation.get("requestBody", {}).get("content", {})
    schema = content.get("application/json", {}).get("schema")
    if not isinstance(schema, dict):
        return None
    body = _example(schema, components)
    if isinstance(body, dict) and set(body) >= {"id", "data"}:
        slug = re.sub(r"[^a-z0-9]+", "-", str(operation.get("summary", "resource")).lower()).strip("-")
        body["id"] = f"{slug}-001"
        if path == "/v1/simulations":
            body["data"] = {"seed": 7, "duration_hours": 24, "step_minutes": 15, "gpu_count": 16}
        elif path == "/v1/jobs":
            body["data"] = {"priority": 50}
    if path.endswith("/evidence-request") and isinstance(body, dict):
        names = (
            "oidc", "kubernetes", "slurm", "meter", "ems", "production_load",
            "penetration_test", "postgres_restore", "object_restore", "product_owner",
            "security_owner", "operations_owner",
        )
        body["requirements"] = {name: {"required": True} for name in names}
    return body


def build_catalog(openapi: dict[str, Any]) -> dict[str, Any]:
    components = openapi.get("components", {})
    by_batch: dict[str, list[dict[str, Any]]] = {skill_id: [] for skill_id, _, _ in SKILLS}
    seen: set[str] = set()
    for path, path_item in sorted(openapi.get("paths", {}).items()):
        for method, operation in sorted(path_item.items()):
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            verb = method.upper()
            operation_id = str(operation.get("operationId") or f"{method}-{path}")
            if operation_id in seen:
                raise ValueError(f"duplicate OpenAPI operation id: {operation_id}")
            seen.add(operation_id)
            parameters: list[dict[str, Any]] = []
            required_headers: list[str] = []
            for parameter in operation.get("parameters", []):
                if not isinstance(parameter, dict):
                    continue
                location = str(parameter.get("in"))
                name = str(parameter.get("name"))
                if location == "header":
                    if parameter.get("required"):
                        required_headers.append(name)
                    continue
                schema = dict(parameter.get("schema", {}))
                parameters.append(
                    {
                        "name": name,
                        "location": location,
                        "required": bool(parameter.get("required")),
                        "default": _parameter_default(name, schema),
                    }
                )
            risk = _risk(verb, path)
            body = _body_example(verb, path, operation, components)
            by_batch[_batch_for(verb, path)].append(
                {
                    "id": operation_id,
                    "method": verb,
                    "path": path,
                    "summary": str(operation.get("summary") or operation_id),
                    "risk": risk,
                    "presentation": _presentation(path),
                    "parameters": parameters,
                    "required_headers": sorted(required_headers),
                    "body_example": body,
                    "has_body": body is not None,
                    "requires_confirmation": risk == "high",
                    "audited": verb != "GET" and path.startswith("/v1/"),
                    "compensation": (
                        "Explicit rollback, cancellation or compensation endpoint is available."
                        if any(marker in path for marker in ("/cancel", "/rollback", "/revoke"))
                        else "Server-side policy and state transition rules define recovery behavior."
                    ),
                }
            )
    skills = [
        {"id": skill_id, "title": title, "mission": mission, "operations": by_batch[skill_id]}
        for skill_id, title, mission in SKILLS
    ]
    return {
        "schema_version": "1.0.0",
        "source": "schemas/openapi/openapi.json",
        "operation_count": len(seen),
        "skill_count": len(skills),
        "unmapped_operations": [],
        "skills": skills,
    }


def rendered_catalog(openapi: dict[str, Any] | None = None) -> str:
    document = openapi or json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    return json.dumps(build_catalog(document), indent=2, sort_keys=True) + "\n"


def main() -> None:
    OUTPUT_PATH.write_text(rendered_catalog(), encoding="utf-8")
    print(f"Web workflow catalog: PASS ({json.loads(OUTPUT_PATH.read_text())['operation_count']} operations)")


if __name__ == "__main__":
    main()
