from __future__ import annotations

from apps.api.main import app
from scripts.export_web_workflows import build_catalog


def test_web_catalog_covers_every_openapi_operation_once() -> None:
    openapi = app.openapi()
    catalog = build_catalog(openapi)
    expected = {
        str(operation["operationId"])
        for path_item in openapi["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    }
    rendered = [operation for skill in catalog["skills"] for operation in skill["operations"]]
    assert catalog["skill_count"] == 20
    assert catalog["unmapped_operations"] == []
    assert len(rendered) == len(expected) == catalog["operation_count"]
    assert {operation["id"] for operation in rendered} == expected
    assert {skill["id"] for skill in catalog["skills"]} == {f"B{index:02d}" for index in range(1, 21)}


def test_every_mutation_exposes_governance_controls() -> None:
    catalog = build_catalog(app.openapi())
    operations = [operation for skill in catalog["skills"] for operation in skill["operations"]]
    writes = [operation for operation in operations if operation["method"] != "GET"]
    assert writes
    assert all(operation["audited"] for operation in writes)
    assert all("Idempotency-Key" in operation["required_headers"] for operation in writes)
    assert all(operation["compensation"] for operation in writes)
    assert all(operation["requires_confirmation"] for operation in writes if operation["risk"] == "high")


def test_production_certification_frontend_has_complete_lifecycle_operations() -> None:
    catalog = build_catalog(app.openapi())
    b20 = next(skill for skill in catalog["skills"] if skill["id"] == "B20")
    routes = {(operation["method"], operation["path"]) for operation in b20["operations"]}
    assert {
        ("GET", "/v1/certification/{release_id}"),
        ("GET", "/v1/certification/{release_id}/external-readiness"),
        ("GET", "/v1/certification/{release_id}/events"),
        ("POST", "/v1/certification/{release_id}/evidence-request"),
        ("POST", "/v1/certification/{release_id}/run"),
        ("POST", "/v1/certification/{release_id}/publish"),
        ("POST", "/v1/certification/{release_id}/revoke"),
    } <= routes
