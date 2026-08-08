from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .evidence import verify_evidence
from .requests import load_verified_request

CommandRunner = Callable[[list[str], int], subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class SuiteStep:
    name: str
    status: str
    evidence: str | None
    reason: str | None
    exit_code: int | None


@dataclass(frozen=True, slots=True)
class ExternalGateSuiteReport:
    status: str
    release_id: str | None
    source_revision: str | None
    request_sha256: str | None
    steps: tuple[SuiteStep, ...]

    def as_document(self) -> dict[str, object]:
        return {
            "status": self.status,
            "release_id": self.release_id,
            "source_revision": self.source_revision,
            "request_sha256": self.request_sha256,
            "checks": [asdict(step) for step in self.steps],
        }


def _default_runner(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - Python executable plus internally assembled module arguments
        command,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _path(base: Path, value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else (base / path).resolve()


def _verified_pass(
    path: Path,
    *,
    release_id: str,
    source_revision: str,
    request_sha256: str,
    request_bound: bool = True,
) -> tuple[bool, str | None]:
    integrity, reason = verify_evidence(path)
    if not integrity:
        return False, reason
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "gate evidence is not valid JSON"
    if not isinstance(document, dict) or document.get("status") != "PASS":
        return False, "gate evidence status is not PASS"
    if document.get("source_revision") != source_revision:
        return False, "gate evidence source revision is not bound"
    if request_bound and (
        document.get("release_id") != release_id or document.get("request_sha256") != request_sha256
    ):
        return False, "gate evidence request binding does not match"
    return True, None


def _overall(steps: list[SuiteStep]) -> str:
    if steps and all(step.status == "PASS" for step in steps):
        return "PASS"
    if any(step.status == "FAIL" for step in steps):
        return "FAIL"
    return "NOT_RUN"


def run_external_gate_suite(
    root: Path,
    configuration: dict[str, Any],
    *,
    configuration_base: Path | None = None,
    evidence_root: Path | None = None,
    runner: CommandRunner = _default_runner,
) -> ExternalGateSuiteReport:
    """Run every real production gate and accept only integrity-protected, request-bound output."""

    base = (configuration_base or root).resolve()
    output_root = (evidence_root or root / "evidence").resolve()
    raw_request = configuration.get("evidence_request")
    if not raw_request:
        step = SuiteStep("evidence_request", "NOT_RUN", None, "evidence_request is not configured", None)
        return ExternalGateSuiteReport("NOT_RUN", None, None, None, (step,))
    request_path = _path(base, raw_request)
    if not request_path.is_file():
        step = SuiteStep("evidence_request", "NOT_RUN", str(request_path), "evidence request is missing", None)
        return ExternalGateSuiteReport("NOT_RUN", None, None, None, (step,))
    try:
        request = load_verified_request(request_path)
    except (KeyError, OSError, ValueError) as error:
        step = SuiteStep("evidence_request", "FAIL", str(request_path), str(error), None)
        return ExternalGateSuiteReport("FAIL", None, None, None, (step,))

    timeout = int(configuration.get("command_timeout_seconds", 3600))
    if timeout < 1 or timeout > 21600:
        raise ValueError("command_timeout_seconds must be between 1 and 21600")
    b20 = output_root / "B20"
    definitions: list[tuple[str, str, Path, bool, list[str] | None]] = []

    def configured_step(name: str, key: str, output_name: str, request_bound: bool, command: list[str]) -> None:
        value = configuration.get(key)
        path = _path(base, value) if value else Path("")
        definitions.append(
            (name, str(path) if value else "", b20 / output_name, request_bound, command if value else None)
        )

    preflight_value = configuration.get("preflight_configuration")
    preflight_path = _path(base, preflight_value) if preflight_value else Path("")
    configured_step(
        "production_preflight",
        "preflight_configuration",
        "production-preflight.json",
        False,
        [
            sys.executable,
            "-m",
            "scripts.run_production_preflight",
            str(preflight_path),
            "--output",
            str(b20 / "production-preflight.json"),
        ],
    )
    acceptance_value = configuration.get("acceptance_manifest")
    acceptance_path = _path(base, acceptance_value) if acceptance_value else Path("")
    configured_step(
        "external_integrations",
        "acceptance_manifest",
        "external-integrations.json",
        True,
        [
            sys.executable,
            "-m",
            "scripts.run_external_acceptance",
            str(acceptance_path),
            "--request",
            str(request_path),
            "--output",
            str(b20 / "external-integrations.json"),
        ],
    )
    restore_value = configuration.get("restore_configuration")
    restore_path = _path(base, restore_value) if restore_value else Path("")
    configured_step(
        "backup_restore",
        "restore_configuration",
        "restore-rehearsal.json",
        True,
        [
            sys.executable,
            "-m",
            "scripts.run_restore_rehearsal",
            str(restore_path),
            "--request",
            str(request_path),
            "--output",
            str(b20 / "restore-rehearsal.json"),
        ],
    )
    bundle_value = configuration.get("attestation_bundle")
    policy_value = configuration.get("attestation_policy")
    bundle_path = _path(base, bundle_value) if bundle_value else Path("")
    policy_path = _path(base, policy_value) if policy_value else Path("")
    attestation_command = [
        sys.executable,
        "-m",
        "scripts.run_production_gates",
        "attestations",
        str(bundle_path),
        "--request",
        str(request_path),
        "--policy",
        str(policy_path),
        "--output",
        str(b20 / "signed-attestations.json"),
    ]
    attestation_input = f"{bundle_path}|{policy_path}" if bundle_value and policy_value else ""
    definitions.append(
        (
            "security_and_acceptance",
            attestation_input,
            b20 / "signed-attestations.json",
            True,
            attestation_command if attestation_input else None,
        )
    )

    load_configuration = configuration.get("production_load")
    load_command: list[str] | None = None
    if isinstance(load_configuration, dict):
        try:
            load_command = [
                sys.executable,
                "-m",
                "scripts.run_production_gates",
                "load",
                str(load_configuration["target"]),
                "--requests",
                str(load_configuration["requests"]),
                "--concurrency",
                str(load_configuration["concurrency"]),
                "--p95-ms",
                str(load_configuration["p95_ms"]),
                "--p99-ms",
                str(load_configuration["p99_ms"]),
                "--max-error-rate",
                str(load_configuration["max_error_rate"]),
                "--release-id",
                request.release_id,
                "--source-revision",
                request.source_revision,
                "--request",
                str(request_path),
                "--output",
                str(b20 / "production-performance-report.json"),
            ]
            for key, flag in (
                ("token_ref", "--token-ref"),
                ("ca_bundle_ref", "--ca-bundle-ref"),
                ("client_certificate_ref", "--client-certificate-ref"),
                ("client_key_ref", "--client-key-ref"),
            ):
                if load_configuration.get(key):
                    load_command.extend([flag, str(load_configuration[key])])
        except KeyError:
            load_command = None
    definitions.insert(
        2,
        (
            "production_load",
            "configured" if isinstance(load_configuration, dict) else "",
            b20 / "production-performance-report.json",
            True,
            load_command,
        ),
    )

    steps: list[SuiteStep] = []
    for name, input_description, output, request_bound, command in definitions:
        if command is None:
            steps.append(SuiteStep(name, "NOT_RUN", None, "gate configuration is incomplete", None))
            continue
        input_paths = [Path(value) for value in input_description.split("|") if value and value != "configured"]
        if input_paths and any(not path.is_file() for path in input_paths):
            steps.append(SuiteStep(name, "NOT_RUN", str(output), "gate input is missing", None))
            continue
        try:
            completed = runner(command, timeout)
        except subprocess.TimeoutExpired:
            steps.append(SuiteStep(name, "FAIL", str(output), "gate command timed out", None))
            continue
        if completed.returncode != 0:
            steps.append(SuiteStep(name, "FAIL", str(output), "gate command exited non-zero", completed.returncode))
            continue
        valid, reason = _verified_pass(
            output,
            release_id=request.release_id,
            source_revision=request.source_revision,
            request_sha256=request.request_sha256,
            request_bound=request_bound,
        )
        steps.append(SuiteStep(name, "PASS" if valid else "FAIL", str(output), reason, completed.returncode))
    return ExternalGateSuiteReport(
        _overall(steps),
        request.release_id,
        request.source_revision,
        request.request_sha256,
        tuple(steps),
    )
