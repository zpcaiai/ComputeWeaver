from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .evidence import verify_evidence
from .requests import load_verified_request
from .service import evaluate_production_evidence

EXTERNAL_GATE_NAMES = (
    "security",
    "performance",
    "backup_restore",
    "external_integrations",
    "acceptance",
)

NEXT_ACTIONS = {
    "security": "verify an independent penetration attestation with make verify-attestations",
    "performance": "run the approved non-local load contract with make production-load",
    "backup_restore": "run isolated database and object recovery with make restore-rehearsal",
    "external_integrations": "probe real IdP, Kubernetes, Slurm, meter and EMS with make external-acceptance",
    "acceptance": "verify distinct product, security and operations owner signatures with make verify-attestations",
}


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    name: str
    status: str
    evidence: tuple[str, ...]
    reason: str | None
    next_action: str | None


@dataclass(frozen=True, slots=True)
class ExternalReadinessReport:
    status: str
    release_id: str
    source_revision: str
    request_sha256: str | None
    checks: tuple[ReadinessCheck, ...]

    def as_document(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "release_id": self.release_id,
            "source_revision": self.source_revision,
            "request_sha256": self.request_sha256,
            "checks": [asdict(check) for check in self.checks],
        }


def _request_check(evidence_root: Path, release_id: str, source_revision: str) -> tuple[ReadinessCheck, str | None]:
    path = evidence_root / "B20" / "evidence-request.json"
    reference = "evidence/B20/evidence-request.json"
    if not path.is_file():
        return (
            ReadinessCheck(
                "evidence_request",
                "NOT_RUN",
                (reference,),
                "release-bound external evidence request is missing",
                "create the immutable request with make evidence-request",
            ),
            None,
        )
    try:
        request = load_verified_request(path)
    except (KeyError, OSError, ValueError) as error:
        return (
            ReadinessCheck(
                "evidence_request",
                "FAIL",
                (reference,),
                str(error),
                "replace the invalid request with make evidence-request",
            ),
            None,
        )
    if request.release_id != release_id or request.source_revision != source_revision:
        return (
            ReadinessCheck(
                "evidence_request",
                "FAIL",
                (reference,),
                "evidence request is bound to a different release or source revision",
                "create a new request for the current release commit",
            ),
            None,
        )
    return ReadinessCheck("evidence_request", "PASS", (reference,), None, None), request.request_sha256


def _preflight_check(evidence_root: Path, source_revision: str) -> ReadinessCheck:
    path = evidence_root / "B20" / "production-preflight.json"
    reference = "evidence/B20/production-preflight.json"
    if not path.is_file():
        return ReadinessCheck(
            "production_preflight",
            "NOT_RUN",
            (reference,),
            "production preflight has not been run",
            "run PRODUCTION_PREFLIGHT_CONFIG=... make production-preflight",
        )
    integrity, reason = verify_evidence(path)
    if not integrity:
        return ReadinessCheck(
            "production_preflight",
            "FAIL",
            (reference,),
            reason,
            "rerun production preflight and preserve its SHA-256 sidecar",
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        document = None
    if not isinstance(document, dict):
        return ReadinessCheck("production_preflight", "FAIL", (reference,), "preflight is invalid", None)
    if document.get("source_revision") != source_revision:
        return ReadinessCheck(
            "production_preflight",
            "FAIL",
            (reference,),
            "production preflight is bound to a different source revision",
            "rerun production preflight for the current commit",
        )
    status = str(document.get("status", "FAIL"))
    return ReadinessCheck(
        "production_preflight",
        status if status in {"PASS", "FAIL", "NOT_RUN"} else "FAIL",
        (reference,),
        None if status == "PASS" else "one or more production prerequisites are not satisfied",
        None if status == "PASS" else "resolve failed preflight checks before external execution",
    )


def evaluate_external_readiness(
    evidence_root: Path,
    *,
    release_id: str,
    source_revision: str,
) -> ExternalReadinessReport:
    request_check, request_sha256 = _request_check(evidence_root, release_id, source_revision)
    preflight = _preflight_check(evidence_root, source_revision)
    try:
        gates = evaluate_production_evidence(
            evidence_root,
            release_id=release_id,
            source_revision=source_revision,
        )
        gate_map = {gate.name: gate for gate in gates}
        external_checks = tuple(
            ReadinessCheck(
                name,
                "PASS" if gate_map[name].passed else "FAIL",
                gate_map[name].evidence,
                gate_map[name].reason,
                None if gate_map[name].passed else NEXT_ACTIONS[name],
            )
            for name in EXTERNAL_GATE_NAMES
        )
    except (FileNotFoundError, KeyError, OSError, ValueError) as error:
        external_checks = tuple(
            ReadinessCheck(name, "NOT_RUN", (), f"certification evidence is incomplete: {error}", NEXT_ACTIONS[name])
            for name in EXTERNAL_GATE_NAMES
        )
    checks = (request_check, preflight, *external_checks)
    statuses = {check.status for check in checks}
    status = "PASS" if statuses == {"PASS"} else "FAIL" if "FAIL" in statuses else "NOT_RUN"
    return ExternalReadinessReport(status, release_id, source_revision, request_sha256, checks)
