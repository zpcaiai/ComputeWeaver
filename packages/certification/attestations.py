from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt

from .requests import IMMUTABLE_REVISION

DIGEST = re.compile(r"^[0-9a-f]{64}$")
HUMAN_ROLES = frozenset({"product_owner", "security_owner", "operations_owner"})


@dataclass(frozen=True, slots=True)
class AttestationResult:
    kind: str
    status: str
    issuer: str | None
    subject: str | None
    expires_at: datetime | None
    claims: dict[str, Any]
    reason: str | None = None


def _not_run(kind: str, reason: str) -> AttestationResult:
    return AttestationResult(kind, "NOT_RUN", None, None, None, {}, reason)


def verify_attestation(
    *,
    kind: str,
    token_path: Path,
    public_key_path: Path,
    expected_release_id: str,
    expected_source_revision: str,
    trusted_issuer: str,
    approved_subjects: frozenset[str],
    release_requester: str | None = None,
    expected_request_sha256: str | None = None,
    expected_artifact_sha256: str | None = None,
) -> AttestationResult:
    if not token_path.is_file() or not public_key_path.is_file():
        return _not_run(kind, "signed attestation or public key is missing")
    try:
        token = token_path.read_text(encoding="utf-8").strip()
        claims: dict[str, Any] = jwt.decode(
            token,
            public_key_path.read_bytes(),
            algorithms=["RS256", "ES256", "EdDSA"],
            audience="computeweaver-release-gate",
            issuer=trusted_issuer,
            options={
                "require": [
                    "exp",
                    "iat",
                    "iss",
                    "aud",
                    "sub",
                    "jti",
                    "kind",
                    "release_id",
                    "source_revision",
                    "status",
                    "request_sha256",
                    "artifact_sha256",
                ]
            },
            leeway=30,
        )
        subject = str(claims["sub"])
        if claims.get("kind") != kind:
            raise ValueError("attestation kind mismatch")
        if claims.get("release_id") != expected_release_id:
            raise ValueError("attestation release mismatch")
        if claims.get("source_revision") != expected_source_revision:
            raise ValueError("attestation source revision mismatch")
        request_sha256 = str(claims.get("request_sha256"))
        artifact_sha256 = str(claims.get("artifact_sha256"))
        if not DIGEST.fullmatch(request_sha256) or not DIGEST.fullmatch(artifact_sha256):
            raise ValueError("attestation evidence digests are invalid")
        if expected_request_sha256 and request_sha256 != expected_request_sha256:
            raise ValueError("attestation evidence request digest mismatch")
        if expected_artifact_sha256 and artifact_sha256 != expected_artifact_sha256:
            raise ValueError("attestation artifact digest mismatch")
        if subject not in approved_subjects:
            raise PermissionError("attestation signer is not approved")
        if release_requester and subject == release_requester:
            raise PermissionError("release requester cannot independently attest the release")
        if claims.get("status") != "PASS":
            raise ValueError("attestation does not record PASS")
        if kind == "penetration_test":
            findings = claims.get("open_findings")
            if not isinstance(findings, dict):
                raise ValueError("penetration test finding summary is missing")
            if int(findings.get("critical", -1)) != 0 or int(findings.get("high", -1)) != 0:
                raise ValueError("penetration test has unresolved critical/high findings")
        elif kind == "human_acceptance":
            decisions = claims.get("decisions")
            if not isinstance(decisions, list):
                raise ValueError("human acceptance decisions are missing")
            accepted_roles = {
                str(item.get("role"))
                for item in decisions
                if isinstance(item, dict) and item.get("decision") == "ACCEPT"
            }
            actors = [str(item.get("actor")) for item in decisions if isinstance(item, dict)]
            if not HUMAN_ROLES.issubset(accepted_roles):
                raise ValueError("human acceptance is missing a required owner role")
            if len(actors) != len(set(actors)) or release_requester in actors:
                raise PermissionError("human acceptance violates separation of duties")
        elif kind == "human_acceptance_vote":
            role = str(claims.get("role"))
            actor = str(claims.get("actor"))
            if role not in HUMAN_ROLES or claims.get("decision") != "ACCEPT" or not actor:
                raise ValueError("human acceptance vote is invalid")
            if actor != subject:
                raise PermissionError("human acceptance vote actor must match signing subject")
            if release_requester and actor == release_requester:
                raise PermissionError("release requester cannot accept their own release")
        expires_at = datetime.fromtimestamp(float(claims["exp"]), tz=UTC)
        safe_claims = {key: value for key, value in claims.items() if key not in {"token", "credential", "secret"}}
        safe_claims["token_sha256"] = hashlib.sha256(token_path.read_bytes()).hexdigest()
        return AttestationResult(kind, "PASS", trusted_issuer, subject, expires_at, safe_claims)
    except (OSError, ValueError, PermissionError, jwt.PyJWTError) as error:
        return AttestationResult(kind, "FAIL", None, None, None, {}, f"{type(error).__name__}: {error}")


def issue_attestation(
    *,
    kind: str,
    issuer: str,
    subject: str,
    private_key_path: Path,
    release_id: str,
    source_revision: str,
    request_sha256: str,
    artifact_sha256: str,
    claims: dict[str, Any],
    key_id: str | None = None,
    now: datetime | None = None,
    valid_for: timedelta = timedelta(days=7),
) -> str:
    issued_at = now or datetime.now(UTC)
    if issued_at.tzinfo is None:
        raise ValueError("attestation issue time must be timezone-aware")
    if not IMMUTABLE_REVISION.fullmatch(source_revision):
        raise ValueError("attestations require an immutable source revision")
    if kind not in {"penetration_test", "human_acceptance", "human_acceptance_vote"}:
        raise ValueError("unsupported attestation kind")
    if not all((issuer, subject, release_id)):
        raise ValueError("attestation identity and release fields are required")
    if not DIGEST.fullmatch(request_sha256) or not DIGEST.fullmatch(artifact_sha256):
        raise ValueError("attestation evidence digests are invalid")
    if valid_for <= timedelta(0) or valid_for > timedelta(days=30):
        raise ValueError("attestation validity must be within 30 days")
    file_mode = stat.S_IMODE(private_key_path.stat().st_mode)
    if file_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError("attestation private key must not be group/world accessible")
    if kind == "penetration_test":
        findings = claims.get("open_findings")
        if not isinstance(findings, dict):
            raise ValueError("penetration test finding summary is required")
    if kind == "human_acceptance_vote":
        if claims.get("role") not in HUMAN_ROLES or claims.get("decision") != "ACCEPT":
            raise ValueError("human acceptance vote requires an approved owner role and ACCEPT")
        if claims.get("actor") != subject:
            raise PermissionError("human acceptance vote actor must match signing subject")
    payload = {
        "iss": issuer,
        "aud": "computeweaver-release-gate",
        "sub": subject,
        "iat": issued_at,
        "exp": issued_at + valid_for,
        "jti": hashlib.sha256(
            f"{issuer}:{subject}:{kind}:{release_id}:{request_sha256}:{issued_at.isoformat()}".encode()
        ).hexdigest(),
        "kind": kind,
        "release_id": release_id,
        "source_revision": source_revision,
        "request_sha256": request_sha256,
        "artifact_sha256": artifact_sha256,
        "status": "PASS",
        **claims,
    }
    headers = {"kid": key_id} if key_id else None
    return jwt.encode(payload, private_key_path.read_bytes(), algorithm="ES256", headers=headers)


def issue_attestation_from_config(configuration: dict[str, Any]) -> str:
    from .evidence import verify_evidence
    from .requests import request_from_document, verify_evidence_request

    request_path = Path(str(configuration["request_file"]))
    request_integrity, request_error = verify_evidence(request_path)
    if not request_integrity:
        raise ValueError(f"evidence request integrity check failed: {request_error}")
    request_document = json.loads(request_path.read_text(encoding="utf-8"))
    request = request_from_document(request_document)
    if not verify_evidence_request(request):
        raise ValueError("evidence request is expired or its request digest is invalid")
    artifact_path = Path(str(configuration["artifact_file"]))
    if not artifact_path.is_file():
        raise FileNotFoundError(artifact_path)
    artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    for field, actual in (
        ("release_id", request.release_id),
        ("source_revision", request.source_revision),
        ("request_sha256", request.request_sha256),
        ("artifact_sha256", artifact_sha256),
    ):
        configured = configuration.get(field)
        if configured is not None and str(configured) != actual:
            raise ValueError(f"attestation {field} does not match its bound evidence")
    return issue_attestation(
        kind=str(configuration["kind"]),
        issuer=str(configuration["issuer"]),
        subject=str(configuration["subject"]),
        private_key_path=Path(str(configuration["private_key_file"])),
        release_id=request.release_id,
        source_revision=request.source_revision,
        request_sha256=request.request_sha256,
        artifact_sha256=artifact_sha256,
        claims=dict(configuration.get("claims", {})),
        key_id=str(configuration["key_id"]) if configuration.get("key_id") else None,
        valid_for=timedelta(hours=int(configuration.get("valid_for_hours", 168))),
    )


def write_attestation(path: Path, token: str) -> str:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(token + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256((token + "\n").encode()).hexdigest()


def _artifact_digest(raw: dict[str, Any]) -> str:
    artifact_path = Path(str(raw["artifact_file"]))
    if not artifact_path.is_file():
        raise FileNotFoundError(artifact_path)
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    configured = raw.get("artifact_sha256")
    if configured is not None and configured != digest:
        raise ValueError("configured artifact digest does not match the artifact file")
    return digest


def _apply_trust_policy(configuration: dict[str, Any], trust_policy: dict[str, Any]) -> dict[str, Any]:
    resolved = deepcopy(configuration)
    penetration = resolved.get("penetration_test")
    penetration_policy = trust_policy.get("penetration_test")
    if isinstance(penetration, dict):
        if not isinstance(penetration_policy, dict):
            raise ValueError("penetration-test trust policy is missing")
        for field in ("trusted_issuer", "public_key_file", "approved_subjects"):
            if not penetration_policy.get(field):
                raise ValueError(f"penetration-test trust policy {field} is missing")
            penetration[field] = penetration_policy[field]
    human = resolved.get("human_acceptance")
    owner_policies = trust_policy.get("human_acceptance")
    if isinstance(human, dict):
        signatures = human.get("signatures")
        if not isinstance(signatures, list) or not isinstance(owner_policies, dict):
            raise ValueError("human-acceptance trust policy or signatures are missing")
        if set(owner_policies) != HUMAN_ROLES:
            raise ValueError("trust policy must define exactly the three required owner roles")
        for entry in signatures:
            if not isinstance(entry, dict):
                raise ValueError("human-acceptance signature entry is invalid")
            role = str(entry.get("role", ""))
            policy = owner_policies.get(role)
            if not isinstance(policy, dict):
                raise ValueError(f"trust policy for {role or 'unknown role'} is missing")
            for field in ("trusted_issuer", "public_key_file", "approved_subjects"):
                if not policy.get(field):
                    raise ValueError(f"human-acceptance trust policy {role}.{field} is missing")
                entry[field] = policy[field]
    return resolved


def _human_acceptance_result(
    raw: dict[str, Any],
    *,
    release_id: str,
    source_revision: str,
    release_requester: str | None,
    request_sha256: str,
) -> AttestationResult:
    signatures = raw.get("signatures")
    if not isinstance(signatures, list) or not signatures:
        return _not_run("human_acceptance", "three independently signed owner votes are missing")
    votes: list[AttestationResult] = []
    configured_roles: list[str] = []
    for entry in signatures:
        if not isinstance(entry, dict):
            return AttestationResult(
                "human_acceptance", "FAIL", None, None, None, {}, "human acceptance entry is invalid"
            )
        try:
            artifact_sha256 = _artifact_digest(entry)
            vote = verify_attestation(
                kind="human_acceptance_vote",
                token_path=Path(str(entry["token_file"])),
                public_key_path=Path(str(entry["public_key_file"])),
                expected_release_id=release_id,
                expected_source_revision=source_revision,
                trusted_issuer=str(entry["trusted_issuer"]),
                approved_subjects=frozenset(str(item) for item in entry.get("approved_subjects", [])),
                release_requester=release_requester,
                expected_request_sha256=request_sha256,
                expected_artifact_sha256=artifact_sha256,
            )
        except (KeyError, OSError, ValueError) as error:
            vote = AttestationResult(
                "human_acceptance_vote", "FAIL", None, None, None, {}, f"{type(error).__name__}: {error}"
            )
        configured_role = str(entry.get("role", ""))
        if vote.status == "PASS" and configured_role and vote.claims.get("role") != configured_role:
            vote = AttestationResult(
                "human_acceptance_vote",
                "FAIL",
                vote.issuer,
                vote.subject,
                vote.expires_at,
                vote.claims,
                "configured owner role does not match the signed role",
            )
        configured_roles.append(str(vote.claims.get("role", configured_role)))
        votes.append(vote)
    failures = [vote for vote in votes if vote.status != "PASS"]
    subjects = [str(vote.subject) for vote in votes if vote.subject]
    role_set = set(configured_roles)
    if failures:
        reason = "; ".join(vote.reason or "owner vote did not pass" for vote in failures)
        return AttestationResult("human_acceptance", "FAIL", None, None, None, {}, reason)
    if len(votes) != len(HUMAN_ROLES) or len(configured_roles) != len(role_set) or role_set != HUMAN_ROLES:
        return AttestationResult(
            "human_acceptance", "FAIL", None, None, None, {}, "exactly one vote from each owner role is required"
        )
    if len(subjects) != len(set(subjects)):
        return AttestationResult(
            "human_acceptance", "FAIL", None, None, None, {}, "owner votes must have distinct signing subjects"
        )
    expiries = [vote.expires_at for vote in votes if vote.expires_at]
    issuers = sorted({str(vote.issuer) for vote in votes})
    return AttestationResult(
        "human_acceptance",
        "PASS",
        ",".join(issuers),
        ",".join(sorted(subjects)),
        min(expiries) if expiries else None,
        {
            "request_sha256": request_sha256,
            "decisions": [
                {
                    "role": vote.claims["role"],
                    "actor": vote.subject,
                    "decision": vote.claims["decision"],
                    "artifact_sha256": vote.claims["artifact_sha256"],
                    "token_sha256": vote.claims["token_sha256"],
                }
                for vote in votes
            ],
        },
    )


def verify_attestation_bundle(
    configuration: dict[str, Any],
    *,
    expected_request_sha256: str | None = None,
    trust_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if trust_policy is not None:
        configuration = _apply_trust_policy(configuration, trust_policy)
    release_id = str(configuration["release_id"])
    source_revision = str(configuration["source_revision"])
    release_requester = str(configuration.get("release_requester", "")) or None
    request_sha256 = str(configuration.get("request_sha256", ""))
    if expected_request_sha256 and request_sha256 != expected_request_sha256:
        request_sha256 = ""
    results: list[AttestationResult] = []
    if not DIGEST.fullmatch(request_sha256):
        status = "FAIL" if configuration.get("penetration_test") or configuration.get("human_acceptance") else "NOT_RUN"
        reason = "bundle request digest is missing or does not match the verified evidence request"
        results = [
            AttestationResult(kind, status, None, None, None, {}, reason)
            for kind in ("penetration_test", "human_acceptance")
        ]
    else:
        raw = configuration.get("penetration_test")
        if not isinstance(raw, dict):
            results.append(_not_run("penetration_test", "attestation configuration missing"))
        else:
            try:
                artifact_sha256 = _artifact_digest(raw)
                results.append(
                    verify_attestation(
                        kind="penetration_test",
                        token_path=Path(str(raw["token_file"])),
                        public_key_path=Path(str(raw["public_key_file"])),
                        expected_release_id=release_id,
                        expected_source_revision=source_revision,
                        trusted_issuer=str(raw["trusted_issuer"]),
                        approved_subjects=frozenset(str(item) for item in raw.get("approved_subjects", [])),
                        release_requester=release_requester,
                        expected_request_sha256=request_sha256,
                        expected_artifact_sha256=artifact_sha256,
                    )
                )
            except (KeyError, OSError, ValueError) as error:
                results.append(
                    AttestationResult(
                        "penetration_test", "FAIL", None, None, None, {}, f"{type(error).__name__}: {error}"
                    )
                )
        human = configuration.get("human_acceptance")
        if not isinstance(human, dict):
            results.append(_not_run("human_acceptance", "attestation configuration missing"))
        else:
            results.append(
                _human_acceptance_result(
                    human,
                    release_id=release_id,
                    source_revision=source_revision,
                    release_requester=release_requester,
                    request_sha256=request_sha256,
                )
            )
    statuses = {result.status for result in results}
    overall = "FAIL" if "FAIL" in statuses else "PASS" if statuses == {"PASS"} else "NOT_RUN"
    return {
        "status": overall,
        "release_id": release_id,
        "source_revision": source_revision,
        "request_sha256": request_sha256 or None,
        "cryptographic_verification": True,
        "results": [asdict(item) for item in results],
    }


def load_bundle(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("attestation bundle must be a JSON object")
    return document


def _evidence_reference(path: Path, evidence_root: Path) -> str:
    resolved_root = evidence_root.resolve()
    resolved_path = path.resolve()
    try:
        return resolved_path.relative_to(resolved_root).as_posix()
    except ValueError as error:
        raise ValueError(f"attestation artifact must be stored under {resolved_root}: {path}") from error


def build_verification_material(
    configuration: dict[str, Any],
    *,
    trust_policy_path: Path,
    evidence_root: Path,
) -> dict[str, Any]:
    """Create a portable, non-secret bundle that can be cryptographically reverified.

    Signed JWTs and public keys are safe to preserve as evidence. Private keys and provider
    credentials are never copied. Artifact bodies remain in the evidence tree and are addressed
    by validated relative paths.
    """

    policy_bytes = trust_policy_path.read_bytes()
    trust_policy = json.loads(policy_bytes)
    if not isinstance(trust_policy, dict):
        raise ValueError("attestation trust policy must be a JSON object")
    resolved = _apply_trust_policy(configuration, trust_policy)
    entries: list[dict[str, Any]] = []
    public_keys: dict[str, dict[str, str]] = {}

    def add_entry(raw: dict[str, Any], *, kind: str, role: str | None = None) -> None:
        token_path = Path(str(raw["token_file"]))
        key_path = Path(str(raw["public_key_file"]))
        artifact_path = Path(str(raw["artifact_file"]))
        key_name = key_path.name
        key_bytes = key_path.read_bytes()
        existing = public_keys.get(key_name)
        encoded_key = base64.b64encode(key_bytes).decode("ascii")
        if existing and existing["pem_b64"] != encoded_key:
            raise ValueError(f"attestation public key names must be unique: {key_name}")
        public_keys[key_name] = {
            "pem_b64": encoded_key,
            "sha256": hashlib.sha256(key_bytes).hexdigest(),
        }
        entry: dict[str, Any] = {
            "kind": kind,
            "token": token_path.read_text(encoding="utf-8").strip(),
            "token_sha256": hashlib.sha256(token_path.read_text(encoding="utf-8").strip().encode()).hexdigest(),
            "artifact": _evidence_reference(artifact_path, evidence_root),
            "artifact_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            "public_key_name": key_name,
        }
        if role:
            entry["role"] = role
        entries.append(entry)

    penetration = resolved.get("penetration_test")
    if isinstance(penetration, dict):
        add_entry(penetration, kind="penetration_test")
    human = resolved.get("human_acceptance")
    if isinstance(human, dict):
        signatures = human.get("signatures")
        if isinstance(signatures, list):
            for signature in signatures:
                if not isinstance(signature, dict):
                    raise ValueError("human acceptance signature entry is invalid")
                add_entry(
                    signature,
                    kind="human_acceptance_vote",
                    role=str(signature.get("role", "")),
                )
    return {
        "schema_version": "1.0.0",
        "trust_policy_name": trust_policy_path.name,
        "trust_policy_b64": base64.b64encode(policy_bytes).decode("ascii"),
        "trust_policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
        "public_keys": public_keys,
        "entries": entries,
    }


def verify_verification_material(
    material: dict[str, Any],
    *,
    evidence_root: Path,
    release_id: str,
    source_revision: str,
    release_requester: str,
    request_sha256: str,
    bound_input_artifacts: dict[str, str],
) -> dict[str, Any]:
    """Re-run JWT verification from the portable material embedded in gate evidence."""

    if material.get("schema_version") != "1.0.0":
        raise ValueError("attestation verification material schema is unsupported")
    policy_name = str(material.get("trust_policy_name", ""))
    policy_bytes = base64.b64decode(str(material.get("trust_policy_b64", "")), validate=True)
    policy_digest = hashlib.sha256(policy_bytes).hexdigest()
    if material.get("trust_policy_sha256") != policy_digest:
        raise ValueError("embedded attestation trust policy digest is invalid")
    if bound_input_artifacts.get(policy_name) != policy_digest:
        raise ValueError("embedded attestation trust policy is not bound to the evidence request")
    trust_policy = json.loads(policy_bytes)
    if not isinstance(trust_policy, dict):
        raise ValueError("embedded attestation trust policy must be a JSON object")
    raw_keys = material.get("public_keys")
    entries = material.get("entries")
    if not isinstance(raw_keys, dict) or not isinstance(entries, list):
        raise ValueError("attestation verification material is incomplete")
    kinds = [str(item.get("kind")) for item in entries if isinstance(item, dict)]
    roles = [str(item.get("role")) for item in entries if isinstance(item, dict) and item.get("role")]
    if kinds.count("penetration_test") != 1 or kinds.count("human_acceptance_vote") != len(HUMAN_ROLES):
        raise ValueError("attestation verification material requires one penetration test and three owner votes")
    if set(roles) != HUMAN_ROLES or len(roles) != len(set(roles)):
        raise ValueError("attestation verification material has invalid owner roles")

    with tempfile.TemporaryDirectory(prefix="computeweaver-attestation-") as directory_name:
        directory = Path(directory_name)
        key_paths: dict[str, Path] = {}
        for key_name, raw_key in raw_keys.items():
            if not isinstance(raw_key, dict) or Path(str(key_name)).name != str(key_name):
                raise ValueError("embedded attestation public key entry is invalid")
            key_bytes = base64.b64decode(str(raw_key.get("pem_b64", "")), validate=True)
            key_digest = hashlib.sha256(key_bytes).hexdigest()
            if raw_key.get("sha256") != key_digest:
                raise ValueError("embedded attestation public key digest is invalid")
            if bound_input_artifacts.get(str(key_name)) != key_digest:
                raise ValueError(f"attestation public key is not bound to the evidence request: {key_name}")
            key_path = directory / str(key_name)
            key_path.write_bytes(key_bytes)
            key_paths[str(key_name)] = key_path

        resolved_root = evidence_root.resolve()
        penetration: dict[str, Any] | None = None
        signatures: list[dict[str, Any]] = []
        for index, item in enumerate(entries):
            if not isinstance(item, dict):
                raise ValueError("embedded attestation entry is invalid")
            token = str(item.get("token", ""))
            if hashlib.sha256(token.encode()).hexdigest() != item.get("token_sha256"):
                raise ValueError("embedded attestation token digest is invalid")
            key_name = str(item.get("public_key_name", ""))
            if key_name not in key_paths:
                raise ValueError("embedded attestation public key is missing")
            artifact = (resolved_root / str(item.get("artifact", ""))).resolve()
            try:
                artifact.relative_to(resolved_root)
            except ValueError as error:
                raise ValueError("embedded attestation artifact escapes the evidence root") from error
            if not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != item.get(
                "artifact_sha256"
            ):
                raise ValueError("embedded attestation artifact is missing or changed")
            token_path = directory / f"attestation-{index}.jwt"
            token_path.write_text(token + "\n", encoding="utf-8")
            reconstructed = {
                "token_file": str(token_path),
                "public_key_file": str(key_paths[key_name]),
                "artifact_file": str(artifact),
            }
            if item.get("kind") == "penetration_test":
                penetration = reconstructed
            else:
                reconstructed["role"] = str(item.get("role", ""))
                signatures.append(reconstructed)

        if penetration is None:
            raise ValueError("embedded penetration-test attestation is missing")
        reconstructed_bundle = {
            "release_id": release_id,
            "source_revision": source_revision,
            "release_requester": release_requester,
            "request_sha256": request_sha256,
            "penetration_test": penetration,
            "human_acceptance": {"signatures": signatures},
        }
        reconstructed_policy = deepcopy(trust_policy)
        penetration_policy = reconstructed_policy.get("penetration_test")
        if isinstance(penetration_policy, dict):
            original_name = Path(str(penetration_policy.get("public_key_file", ""))).name
            penetration_policy["public_key_file"] = str(key_paths.get(original_name, ""))
        human_policy = reconstructed_policy.get("human_acceptance")
        if isinstance(human_policy, dict):
            for raw_policy in human_policy.values():
                if isinstance(raw_policy, dict):
                    original_name = Path(str(raw_policy.get("public_key_file", ""))).name
                    raw_policy["public_key_file"] = str(key_paths.get(original_name, ""))
        result = verify_attestation_bundle(
            reconstructed_bundle,
            expected_request_sha256=request_sha256,
            trust_policy=reconstructed_policy,
        )
    result["trust_policy_sha256"] = policy_digest
    return result
