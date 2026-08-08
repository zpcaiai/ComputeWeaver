from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.main import app

CLIENT = TestClient(app)
BASE_HEADERS = {
    "X-Tenant-Id": "tenant-e2e",
    "X-Actor-Id": "operator-e2e",
    "X-Roles": "operator",
}


def _headers(key: str) -> dict[str, str]:
    return {**BASE_HEADERS, "Idempotency-Key": key}


def test_durable_simulation_snapshot_restore_replays_identical_future() -> None:
    created = CLIENT.post(
        "/v1/simulations",
        headers=_headers("e2e-simulation-create-0001"),
        json={"id": "e2e-simulation", "data": {"duration_hours": 1, "seed": 44}},
    )
    assert created.status_code == 200
    CLIENT.post(
        "/v1/simulations/e2e-simulation/step",
        headers=_headers("e2e-simulation-step-0001"),
        json={"fault": "job_burst"},
    ).raise_for_status()
    snapshot = CLIENT.post(
        "/v1/simulations/e2e-simulation/snapshot",
        headers=_headers("e2e-simulation-snapshot-0001"),
        json={},
    )
    assert snapshot.status_code == 200
    future = CLIENT.post(
        "/v1/simulations/e2e-simulation/step",
        headers=_headers("e2e-simulation-step-0002"),
        json={},
    ).json()
    restored = CLIENT.post(
        "/v1/simulations/e2e-simulation/restore",
        headers=_headers("e2e-simulation-restore-0001"),
        json={"snapshot_token": snapshot.json()["snapshot_token"]},
    )
    assert restored.status_code == 200
    replay = CLIENT.post(
        "/v1/simulations/e2e-simulation/step",
        headers=_headers("e2e-simulation-step-0003"),
        json={},
    ).json()
    transient = {"version", "etag"}
    assert {key: value for key, value in future.items() if key not in transient} == {
        key: value for key, value in replay.items() if key not in transient
    }


def test_simulation_operation_replay_is_idempotent() -> None:
    headers = _headers("e2e-simulation-step-0004")
    first = CLIENT.post("/v1/simulations/e2e-simulation/step", headers=headers, json={})
    second = CLIENT.post("/v1/simulations/e2e-simulation/step", headers=headers, json={})
    assert first.status_code == 200
    assert first.json() == second.json()
