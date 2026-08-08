from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .evidence import canonical_json, verify_evidence, write_evidence

REQUIRED_EXTERNAL_GATES = frozenset(
    {
        "oidc",
        "kubernetes",
        "slurm",
        "meter",
        "ems",
        "production_load",
        "penetration_test",
        "postgres_restore",
        "object_restore",
        "product_owner",
        "security_owner",
        "operations_owner",
    }
)
IMMUTABLE_REVISION = re.compile(r"^(?:[0-9a-f]{7,64}|sha256:[0-9a-f]{64})$")


@dataclass(frozen=True, slots=True)
class EvidenceRequest:
    release_id: str
    source_revision: str
    requested_by: str
    issued_at: datetime
    expires_at: datetime
    nonce: str
    requirements: dict[str, Any]
    input_artifacts: dict[str, str]
    request_sha256: str


def _request_digest(body: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(body)).hexdigest()


def create_evidence_request(
    *,
    release_id: str,
    source_revision: str,
    requested_by: str,
    requirements: dict[str, Any],
    input_artifacts: tuple[Path, ...] = (),
    now: datetime | None = None,
    valid_for: timedelta = timedelta(days=7),
    nonce: str | None = None,
) -> EvidenceRequest:
    issued_at = now or datetime.now(UTC)
    if issued_at.tzinfo is None:
        raise ValueError("evidence request time must be timezone-aware")
    if not release_id or not requested_by:
        raise ValueError("release_id and requested_by are required")
    if not IMMUTABLE_REVISION.fullmatch(source_revision):
        raise ValueError("external evidence requests require an immutable source revision")
    if valid_for <= timedelta(0) or valid_for > timedelta(days=30):
        raise ValueError("evidence request validity must be within 30 days")
    missing = REQUIRED_EXTERNAL_GATES - requirements.keys()
    if missing:
        raise ValueError(f"evidence request is missing gate contracts: {', '.join(sorted(missing))}")
    disabled = sorted(
        name
        for name in REQUIRED_EXTERNAL_GATES
        if not isinstance(requirements.get(name), dict) or requirements[name].get("required") is not True
    )
    if disabled:
        raise ValueError(f"mandatory evidence gates cannot be disabled: {', '.join(disabled)}")
    artifacts: dict[str, str] = {}
    for path in sorted(input_artifacts):
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.name in artifacts:
            raise ValueError(f"input artifact names must be unique: {path.name}")
        artifacts[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    body = {
        "release_id": release_id,
        "source_revision": source_revision,
        "requested_by": requested_by,
        "issued_at": issued_at.isoformat(),
        "expires_at": (issued_at + valid_for).isoformat(),
        "nonce": nonce or secrets.token_urlsafe(32),
        "requirements": requirements,
        "input_artifacts": artifacts,
    }
    return EvidenceRequest(
        release_id,
        source_revision,
        requested_by,
        issued_at,
        issued_at + valid_for,
        str(body["nonce"]),
        requirements,
        artifacts,
        _request_digest(body),
    )


def verify_evidence_request(request: EvidenceRequest, *, now: datetime | None = None) -> bool:
    body = {
        "release_id": request.release_id,
        "source_revision": request.source_revision,
        "requested_by": request.requested_by,
        "issued_at": request.issued_at.isoformat(),
        "expires_at": request.expires_at.isoformat(),
        "nonce": request.nonce,
        "requirements": request.requirements,
        "input_artifacts": request.input_artifacts,
    }
    current = now or datetime.now(UTC)
    lifetime = request.expires_at - request.issued_at
    return (
        request.issued_at <= current < request.expires_at
        and timedelta(0) < lifetime <= timedelta(days=30)
        and _request_digest(body) == request.request_sha256
    )


def request_from_document(document: dict[str, Any]) -> EvidenceRequest:
    request = EvidenceRequest(
        release_id=str(document["release_id"]),
        source_revision=str(document["source_revision"]),
        requested_by=str(document["requested_by"]),
        issued_at=datetime.fromisoformat(str(document["issued_at"])),
        expires_at=datetime.fromisoformat(str(document["expires_at"])),
        nonce=str(document["nonce"]),
        requirements=dict(document["requirements"]),
        input_artifacts={str(key): str(value) for key, value in dict(document["input_artifacts"]).items()},
        request_sha256=str(document["request_sha256"]),
    )
    if request.issued_at.tzinfo is None or request.expires_at.tzinfo is None:
        raise ValueError("evidence request timestamps must be timezone-aware")
    if not IMMUTABLE_REVISION.fullmatch(request.source_revision):
        raise ValueError("evidence request source revision is not immutable")
    if REQUIRED_EXTERNAL_GATES - request.requirements.keys():
        raise ValueError("evidence request is missing required gates")
    if any(
        not isinstance(request.requirements.get(name), dict) or request.requirements[name].get("required") is not True
        for name in REQUIRED_EXTERNAL_GATES
    ):
        raise ValueError("evidence request contains a disabled mandatory gate")
    return request


def load_verified_request(path: Path, *, now: datetime | None = None) -> EvidenceRequest:
    integrity, error = verify_evidence(path)
    if not integrity:
        raise ValueError(f"evidence request integrity check failed: {error}")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("evidence request must be a JSON object")
    request = request_from_document(document)
    if not verify_evidence_request(request, now=now):
        raise ValueError("evidence request is expired or its request digest is invalid")
    return request


def create_request_from_config(configuration: dict[str, Any]) -> EvidenceRequest:
    return create_evidence_request(
        release_id=str(configuration["release_id"]),
        source_revision=str(configuration["source_revision"]),
        requested_by=str(configuration["requested_by"]),
        requirements=dict(configuration["requirements"]),
        input_artifacts=tuple(Path(str(item)) for item in configuration.get("input_artifacts", [])),
        valid_for=timedelta(hours=int(configuration.get("valid_for_hours", 168))),
    )


def write_request(path: Path, request: EvidenceRequest, *, command: str) -> str:
    document = json.loads(json.dumps(asdict(request), default=str))
    document["status"] = "PENDING_EXTERNAL_EVIDENCE"
    return write_evidence(path, document, command=command, suite_name="production-evidence-request")
