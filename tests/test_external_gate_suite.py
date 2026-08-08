from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from packages.certification.evidence import write_evidence
from packages.certification.requests import REQUIRED_EXTERNAL_GATES, create_evidence_request, write_request
from packages.certification.suite import run_external_gate_suite


def _request(tmp_path: Path) -> tuple[Path, object]:
    request = create_evidence_request(
        release_id="release-one",
        source_revision="a" * 40,
        requested_by="release-requester",
        requirements={name: {"required": True} for name in REQUIRED_EXTERNAL_GATES},
        now=datetime.now(UTC),
        nonce="suite-test-nonce",
    )
    path = tmp_path / "evidence" / "B20" / "evidence-request.json"
    write_request(path, request, command="test request")
    return path, request


def test_external_gate_suite_is_not_run_when_request_is_missing(tmp_path: Path) -> None:
    report = run_external_gate_suite(tmp_path, {"evidence_request": "missing.json"})
    assert report.status == "NOT_RUN"
    assert report.steps[0].name == "evidence_request"


def test_external_gate_suite_runs_and_verifies_every_bound_output(tmp_path: Path) -> None:
    request_path, request = _request(tmp_path)
    inputs: dict[str, str] = {}
    for name in ("preflight.json", "acceptance.json", "restore.json", "bundle.json", "policy.json"):
        path = tmp_path / name
        path.write_text("{}\n", encoding="utf-8")
        inputs[name] = str(path)
    configuration = {
        "evidence_request": str(request_path),
        "preflight_configuration": inputs["preflight.json"],
        "acceptance_manifest": inputs["acceptance.json"],
        "restore_configuration": inputs["restore.json"],
        "attestation_bundle": inputs["bundle.json"],
        "attestation_policy": inputs["policy.json"],
        "production_load": {
            "target": "https://control.company.test/v1/compute/nodes",
            "requests": 1000,
            "concurrency": 25,
            "p95_ms": 300,
            "p99_ms": 1000,
            "max_error_rate": 0.001,
        },
    }

    def runner(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        assert timeout == 3600
        output = Path(command[command.index("--output") + 1])
        document = {
            "status": "PASS",
            "source_revision": request.source_revision,
            "release_id": request.release_id,
            "request_sha256": request.request_sha256,
        }
        write_evidence(output, document, command="fixture gate", suite_name="fixture")
        return subprocess.CompletedProcess(command, 0, "", "")

    report = run_external_gate_suite(
        tmp_path,
        configuration,
        evidence_root=tmp_path / "evidence",
        runner=runner,
    )
    assert report.status == "PASS"
    assert {step.name for step in report.steps} == {
        "production_preflight",
        "external_integrations",
        "production_load",
        "backup_restore",
        "security_and_acceptance",
    }


def test_external_gate_suite_rejects_nonzero_or_unbound_output(tmp_path: Path) -> None:
    request_path, _ = _request(tmp_path)
    preflight = tmp_path / "preflight.json"
    preflight.write_text("{}", encoding="utf-8")
    configuration = {
        "evidence_request": str(request_path),
        "preflight_configuration": str(preflight),
    }

    def runner(command: list[str], _timeout: int) -> subprocess.CompletedProcess[str]:
        output = Path(command[command.index("--output") + 1])
        write_evidence(
            output,
            {"status": "PASS", "source_revision": "b" * 40},
            command="unbound fixture",
            suite_name="fixture",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    report = run_external_gate_suite(
        tmp_path,
        configuration,
        evidence_root=tmp_path / "evidence",
        runner=runner,
    )
    assert report.status == "FAIL"
    assert next(step for step in report.steps if step.name == "production_preflight").status == "FAIL"
    assert all(step.status == "NOT_RUN" for step in report.steps[1:])
