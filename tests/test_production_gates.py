from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from minio.error import S3Error

from packages.certification import external
from packages.certification.attestations import (
    build_verification_material,
    issue_attestation,
    issue_attestation_from_config,
    verify_attestation,
    verify_attestation_bundle,
    verify_verification_material,
    write_attestation,
)
from packages.certification.evidence import verify_evidence, write_evidence
from packages.certification.load import (
    LoadThresholds,
    percentile,
    run_load_gate,
    validate_load_target,
    validate_production_contract,
)
from packages.certification.requests import (
    REQUIRED_EXTERNAL_GATES,
    create_evidence_request,
    load_verified_request,
    write_request,
)
from packages.certification.service import (
    certify_release,
    collect_gate_evidence_hashes,
    evaluate_production_evidence,
)
from packages.dr import rehearsal
from packages.objectstore.s3 import S3ObjectStore
from packages.secrets import CredentialResolver
from scripts import build_containers

REQUEST_SHA256 = "a" * 64


def _signed_attestation(
    tmp_path: Path,
    kind: str,
    claims: dict[str, Any],
    *,
    subject: str = "independent-signer",
) -> tuple[Path, Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    now = datetime.now(UTC)
    artifact_path = tmp_path / f"{kind}-{subject}.json"
    artifact_path.write_text(json.dumps({"kind": kind, "subject": subject}), encoding="utf-8")
    artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    token = jwt.encode(
        {
            "iss": "assurance-authority",
            "aud": "computeweaver-release-gate",
            "sub": subject,
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": f"attestation-{kind}",
            "kind": kind,
            "release_id": "release-one",
            "source_revision": "revision-one",
            "request_sha256": REQUEST_SHA256,
            "artifact_sha256": artifact_sha256,
            "status": "PASS",
            **claims,
        },
        private,
        algorithm="ES256",
    )
    token_path = tmp_path / f"{kind}-{subject}.jwt"
    key_path = tmp_path / f"{kind}-{subject}.pem"
    token_path.write_text(token, encoding="utf-8")
    key_path.write_bytes(public)
    return token_path, key_path, artifact_path


def test_signed_penetration_and_human_attestations_are_release_bound(tmp_path: Path) -> None:
    penetration_token, penetration_key, penetration_artifact = _signed_attestation(
        tmp_path, "penetration_test", {"open_findings": {"critical": 0, "high": 0, "medium": 1}}
    )
    votes = []
    for role, subject in (
        ("product_owner", "product-one"),
        ("security_owner", "security-one"),
        ("operations_owner", "operations-one"),
    ):
        token, key, artifact = _signed_attestation(
            tmp_path,
            "human_acceptance_vote",
            {"role": role, "actor": subject, "decision": "ACCEPT"},
            subject=subject,
        )
        votes.append(
            {
                "role": role,
                "token_file": str(token),
                "public_key_file": str(key),
                "artifact_file": str(artifact),
                "trusted_issuer": "assurance-authority",
                "approved_subjects": [subject],
            }
        )
    configuration = {
        "release_id": "release-one",
        "source_revision": "revision-one",
        "release_requester": "requester-one",
        "request_sha256": REQUEST_SHA256,
        "penetration_test": {
            "token_file": str(penetration_token),
            "public_key_file": str(penetration_key),
            "artifact_file": str(penetration_artifact),
            "trusted_issuer": "assurance-authority",
            "approved_subjects": ["independent-signer"],
        },
        "human_acceptance": {"signatures": votes},
    }
    result = verify_attestation_bundle(configuration)
    assert result["status"] == "PASS"
    assert result["cryptographic_verification"] is True
    trust_policy = {
        "penetration_test": {
            "trusted_issuer": "assurance-authority",
            "public_key_file": str(penetration_key),
            "approved_subjects": ["independent-signer"],
        },
        "human_acceptance": {
            entry["role"]: {
                "trusted_issuer": entry["trusted_issuer"],
                "public_key_file": entry["public_key_file"],
                "approved_subjects": entry["approved_subjects"],
            }
            for entry in votes
        },
    }
    policy_bound_bundle = json.loads(json.dumps(configuration))
    policy_bound_bundle["penetration_test"].update(
        {
            "trusted_issuer": "attacker",
            "public_key_file": "/attacker/key.pem",
            "approved_subjects": ["attacker"],
        }
    )
    policy_bound = verify_attestation_bundle(policy_bound_bundle, trust_policy=trust_policy)
    assert policy_bound["status"] == "PASS"
    configuration["human_acceptance"]["signatures"].pop()
    incomplete = verify_attestation_bundle(configuration)
    assert incomplete["status"] == "FAIL"
    mismatch = verify_attestation(
        kind="penetration_test",
        token_path=penetration_token,
        public_key_path=penetration_key,
        expected_release_id="different-release",
        expected_source_revision="revision-one",
        trusted_issuer="assurance-authority",
        approved_subjects=frozenset({"independent-signer"}),
    )
    assert mismatch.status == "FAIL"
    missing = verify_attestation_bundle({"release_id": "r", "source_revision": "s"})
    assert missing["status"] == "NOT_RUN"


def test_attestation_verification_material_is_portable_bound_and_reverified(tmp_path: Path) -> None:
    penetration_token, penetration_key, penetration_artifact = _signed_attestation(
        tmp_path, "penetration_test", {"open_findings": {"critical": 0, "high": 0}}
    )
    signatures: list[dict[str, Any]] = []
    owner_policy: dict[str, Any] = {}
    for role, subject in (
        ("product_owner", "product-one"),
        ("security_owner", "security-one"),
        ("operations_owner", "operations-one"),
    ):
        token, key, artifact = _signed_attestation(
            tmp_path,
            "human_acceptance_vote",
            {"role": role, "actor": subject, "decision": "ACCEPT"},
            subject=subject,
        )
        signatures.append({"role": role, "token_file": str(token), "artifact_file": str(artifact)})
        owner_policy[role] = {
            "trusted_issuer": "assurance-authority",
            "public_key_file": str(key),
            "approved_subjects": [subject],
        }
    configuration = {
        "release_id": "release-one",
        "source_revision": "revision-one",
        "release_requester": "requester-one",
        "request_sha256": REQUEST_SHA256,
        "penetration_test": {
            "token_file": str(penetration_token),
            "artifact_file": str(penetration_artifact),
        },
        "human_acceptance": {"signatures": signatures},
    }
    policy = {
        "penetration_test": {
            "trusted_issuer": "assurance-authority",
            "public_key_file": str(penetration_key),
            "approved_subjects": ["independent-signer"],
        },
        "human_acceptance": owner_policy,
    }
    policy_path = tmp_path / "trust-policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    material = build_verification_material(
        configuration,
        trust_policy_path=policy_path,
        evidence_root=tmp_path,
    )
    bound = {
        policy_path.name: hashlib.sha256(policy_path.read_bytes()).hexdigest(),
        **{
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [penetration_key, *(Path(item["public_key_file"]) for item in owner_policy.values())]
        },
    }
    verified = verify_verification_material(
        material,
        evidence_root=tmp_path,
        release_id="release-one",
        source_revision="revision-one",
        release_requester="requester-one",
        request_sha256=REQUEST_SHA256,
        bound_input_artifacts=bound,
    )
    assert verified["status"] == "PASS"
    tampered = json.loads(json.dumps(material))
    tampered["entries"][0]["token"] += "x"
    with pytest.raises(ValueError, match="token digest"):
        verify_verification_material(
            tampered,
            evidence_root=tmp_path,
            release_id="release-one",
            source_revision="revision-one",
            release_requester="requester-one",
            request_sha256=REQUEST_SHA256,
            bound_input_artifacts=bound,
        )


def test_attestation_rejects_findings_and_separation_of_duties(tmp_path: Path) -> None:
    token, key, _ = _signed_attestation(tmp_path, "penetration_test", {"open_findings": {"critical": 0, "high": 1}})
    result = verify_attestation(
        kind="penetration_test",
        token_path=token,
        public_key_path=key,
        expected_release_id="release-one",
        expected_source_revision="revision-one",
        trusted_issuer="assurance-authority",
        approved_subjects=frozenset({"independent-signer"}),
        release_requester="independent-signer",
    )
    assert result.status == "FAIL"


def test_evidence_request_integrity_and_expiry_are_fail_closed(tmp_path: Path) -> None:
    artifact = tmp_path / "image-digest.json"
    artifact.write_text('{"digest":"sha256:abc"}', encoding="utf-8")
    now = datetime.now(UTC)
    request = create_evidence_request(
        release_id="release-one",
        source_revision="abc123def456",
        requested_by="requester-one",
        requirements={name: {"required": True} for name in REQUIRED_EXTERNAL_GATES},
        input_artifacts=(artifact,),
        now=now,
        nonce="deterministic-test-nonce",
    )
    path = tmp_path / "evidence-request.json"
    write_request(path, request, command="certify request")
    assert load_verified_request(path, now=now) == request
    assert verify_evidence(path) == (True, None)
    assert path.with_suffix(".json.junit.xml").is_file()
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert verify_evidence(path)[0] is False
    with pytest.raises(ValueError, match="integrity"):
        load_verified_request(path, now=now)
    with pytest.raises(ValueError, match="missing gate contracts"):
        create_evidence_request(
            release_id="release-one",
            source_revision="abc123def456",
            requested_by="requester-one",
            requirements={},
        )
    with pytest.raises(ValueError, match="immutable"):
        create_evidence_request(
            release_id="release-one",
            source_revision="UNVERSIONED-SOURCE-1234",
            requested_by="requester-one",
            requirements={name: {} for name in REQUIRED_EXTERNAL_GATES},
        )


def test_attestation_issuance_binds_request_artifact_and_protects_private_key(tmp_path: Path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    private_path = tmp_path / "private.pem"
    private_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(private_path, 0o600)
    request = create_evidence_request(
        release_id="release-one",
        source_revision="abc123def456",
        requested_by="requester-one",
        requirements={name: {"required": True} for name in REQUIRED_EXTERNAL_GATES},
        nonce="attestation-issuance-test",
    )
    request_path = tmp_path / "evidence-request.json"
    write_request(request_path, request, command="certify request")
    artifact = tmp_path / "owner-decision.json"
    artifact.write_text('{"decision":"ACCEPT"}', encoding="utf-8")
    artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    token = issue_attestation_from_config(
        {
            "kind": "human_acceptance_vote",
            "issuer": "acceptance-authority",
            "subject": "product-one",
            "private_key_file": str(private_path),
            "request_file": str(request_path),
            "artifact_file": str(artifact),
            "claims": {"role": "product_owner", "actor": "product-one", "decision": "ACCEPT"},
        }
    )
    token_path = tmp_path / "product-owner.jwt"
    digest = write_attestation(token_path, token)
    assert digest == hashlib.sha256((token + "\n").encode()).hexdigest()
    assert token_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_attestation(token_path, token)
    os.chmod(private_path, 0o644)
    with pytest.raises(PermissionError, match="group/world"):
        issue_attestation(
            kind="human_acceptance_vote",
            issuer="acceptance-authority",
            subject="product-one",
            private_key_path=private_path,
            release_id="release-one",
            source_revision="abc123def456",
            request_sha256=request.request_sha256,
            artifact_sha256=artifact_sha256,
            claims={"role": "product_owner", "actor": "product-one", "decision": "ACCEPT"},
        )


def test_load_gate_measures_slos_and_rejects_unsafe_target() -> None:
    latencies = iter([10.0, 20.0, 30.0, 40.0, 50.0])

    def requester(_target: str, _timeout: float) -> tuple[bool, float]:
        return True, next(latencies)

    report = run_load_gate(
        target="http://127.0.0.1:8000/health/ready",
        requests=5,
        concurrency=1,
        thresholds=LoadThresholds(50, 50, 0),
        requester=requester,
    )
    assert report.status == "PASS"
    assert report.p95_ms == 50
    assert not report.production_evidence
    assert percentile([], 0.95) == 0
    with pytest.raises(ValueError, match="must use HTTPS"):
        validate_load_target("http://production.example.com/health")
    failed = run_load_gate(
        target="https://production.example.com/health",
        requests=2,
        concurrency=2,
        thresholds=LoadThresholds(10, 10, 0),
        requester=lambda _target, _timeout: (False, 20),
    )
    assert failed.status == "FAIL"
    assert not failed.production_evidence
    bound = run_load_gate(
        target="https://production.example.com/health",
        requests=1,
        concurrency=1,
        thresholds=LoadThresholds(30, 30, 0),
        requester=lambda _target, _timeout: (True, 20),
        release_id="release-one",
        source_revision="revision-one",
    )
    assert bound.production_evidence
    contract = {
        "target": "https://production.example.com/health",
        "minimum_requests": 1000,
        "minimum_concurrency": 25,
        "maximum_p95_ms": 300,
        "maximum_p99_ms": 1000,
        "maximum_error_rate": 0.001,
    }
    validate_production_contract(
        target=contract["target"],
        requests=1000,
        concurrency=25,
        thresholds=LoadThresholds(300, 1000, 0.001),
        contract=contract,
    )
    with pytest.raises(ValueError, match="below the approved minimum"):
        validate_production_contract(
            target=contract["target"],
            requests=999,
            concurrency=25,
            thresholds=LoadThresholds(300, 1000, 0.001),
            contract=contract,
        )


def test_external_acceptance_aggregates_real_adapter_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        external, "_oidc", lambda config, resolver, tenant_id: {"subject": "operator", "tenant": tenant_id}
    )
    monkeypatch.setattr(
        external,
        "_compute",
        lambda name, config, tenant_id, resolver: {"node_count": 1, "adapter": name},
    )
    monkeypatch.setattr(external, "_meter", lambda config, tenant_id, resolver: {"sample_count": 1})
    monkeypatch.setattr(external, "_ems", lambda config, resolver: {"external_write_executed": False})
    manifest = {
        "tenant_id": "tenant-one",
        "source_revision": "revision-one",
        **{name: {} for name in ("oidc", "kubernetes", "slurm", "meter", "ems")},
    }
    result = external.run_external_acceptance(manifest, resolver=CredentialResolver(Path("/nonexistent")))
    assert result["status"] == "PASS"
    assert result["external_writes_executed"] is False
    partial = external.run_external_acceptance(
        {"tenant_id": "tenant-one", "oidc": {}}, resolver=CredentialResolver(Path("/nonexistent"))
    )
    assert partial["status"] == "NOT_RUN"
    with pytest.raises(ValueError, match="tenant_id"):
        external.run_external_acceptance({}, resolver=CredentialResolver(Path("/nonexistent")))


def test_external_acceptance_helpers_validate_live_protocol_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {
                "issuer": "https://id.example.com",
                "jwks_uri": "https://id.example.com/jwks",
            }

    class Authentication:
        def __init__(self, _settings: object) -> None:
            pass

        def authenticate(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                subject="operator-one",
                tenant_id="tenant-one",
                roles=frozenset({"operator"}),
                token_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )

    monkeypatch.setattr(external.httpx, "get", lambda *args, **kwargs: Response())
    monkeypatch.setattr(external, "Authenticator", Authentication)
    monkeypatch.setenv("COMPUTEWEAVER_CONNECTOR_SECRET_OIDC_TOKEN", "header.payload.signature")
    resolver = CredentialResolver(Path("/nonexistent"))
    oidc = external._oidc(
        {
            "issuer": "https://id.example.com",
            "audience": "computeweaver",
            "jwks_url": "https://id.example.com/jwks",
            "token_ref": "secret://OIDC_TOKEN",
        },
        resolver,
    )
    assert oidc["tenant_id"] == "tenant-one"

    nodes = (SimpleNamespace(tenant_id="tenant-one", site_id="site-one", gpus=(1, 2)),)
    adapter = SimpleNamespace(read_only=True, validate_credentials=lambda: True, discover=lambda: nodes)
    monkeypatch.setattr(external, "create_compute_adapter", lambda *args, **kwargs: adapter)
    assert external._compute("kubernetes", {}, "tenant-one", resolver)["gpu_count"] == 2
    writable_adapter = SimpleNamespace(read_only=False, validate_credentials=lambda: True, discover=lambda: nodes)
    monkeypatch.setattr(external, "create_compute_adapter", lambda *args, **kwargs: writable_adapter)
    with pytest.raises(PermissionError, match="read-only"):
        external._compute("kubernetes", {}, "tenant-one", resolver)

    meter = SimpleNamespace(
        probe=lambda: True,
        pull=lambda **kwargs: ((SimpleNamespace(id="event-one"),), "cursor-two"),
    )
    monkeypatch.setattr(external, "create_meter_connector", lambda *args, **kwargs: meter)
    assert external._meter({}, "tenant-one", resolver)["sample_count"] == 1
    empty_meter = SimpleNamespace(probe=lambda: True, pull=lambda **kwargs: ((), None))
    monkeypatch.setattr(external, "create_meter_connector", lambda *args, **kwargs: empty_meter)
    with pytest.raises(RuntimeError, match="no samples"):
        external._meter({}, "tenant-one", resolver)

    class Executor:
        def __init__(self, settings: object) -> None:
            self.settings = settings

        def dry_run(self, target: str, kind: str, parameters: dict[str, Any]) -> dict[str, Any]:
            return {"valid": target == "ems-one" and kind == "set_power_limit", "parameters": parameters}

    monkeypatch.setattr(external, "GuardedHttpExecutor", Executor)
    monkeypatch.setenv("COMPUTEWEAVER_CONNECTOR_SECRET_EMS_TOKEN", "token")
    ems = external._ems(
        {
            "target": "ems-one",
            "endpoint": "https://ems.example.com",
            "credential_ref": "secret://EMS_TOKEN",
        },
        resolver,
    )
    assert ems["provider_validation"]["valid"]
    assert not ems["external_write_executed"]


def test_external_check_records_failure_without_claiming_not_run() -> None:
    result = external._check("oidc", lambda: (_ for _ in ()).throw(PermissionError("denied")))
    assert result.status == "FAIL"
    assert "denied" in str(result.reason)


def test_container_failure_classification_fallback_and_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    assert build_containers.classify_failure("unexpected EOF")[0] == "NOT_RUN"
    assert build_containers.classify_failure("RUN pip install failed")[0] == "FAIL"
    builds = iter(
        [
            subprocess.CompletedProcess([], 1, "", "unexpected EOF"),
            subprocess.CompletedProcess([], 0, "built", ""),
        ]
    )
    monkeypatch.setattr(
        build_containers,
        "_run",
        lambda command, timeout: subprocess.CompletedProcess(command, 0, "27.0", ""),
    )
    monkeypatch.setattr(
        build_containers,
        "_run_with_progress_timeout",
        lambda command, **kwargs: (next(builds), False, None),
    )
    status, attempts = build_containers.build_images(timeout_seconds=1)
    assert status == "PASS"
    assert [attempt.registry for attempt in attempts] == ["direct", "mirror"]
    monkeypatch.setattr(
        build_containers,
        "_run",
        lambda command, timeout: subprocess.CompletedProcess(command, 0, "uid=65532 paths=10", ""),
    )
    assert build_containers.runtime_smoke("sha256:image")["status"] == "PASS"


def test_restore_targets_and_postgres_rehearsal_are_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = "postgresql://reader@localhost:5432/computeweaver"
    target = "postgresql://restore@localhost:5432/computeweaver_restore_release_one"
    rehearsal.validate_postgres_targets(source, target)
    with pytest.raises(ValueError, match="must start"):
        rehearsal.validate_postgres_targets(source, "postgresql://restore@localhost:5432/not_isolated")
    fingerprint = {"tables": [{"schema": "public", "table": "resources", "rows": 2}], "sha256": "same"}
    fingerprints = iter([{"tables": [], "sha256": "empty"}, fingerprint, fingerprint])
    monkeypatch.setattr(rehearsal, "postgres_fingerprint", lambda _url: next(fingerprints))
    monkeypatch.setattr(rehearsal.shutil, "which", lambda name: f"/usr/bin/{name}")

    def process(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[0].endswith("pg_dump"):
            output = Path(next(item.removeprefix("--file=") for item in command if item.startswith("--file=")))
            output.write_bytes(b"database-backup")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(rehearsal.subprocess, "run", process)
    result = rehearsal.rehearse_postgres_restore(source, target)
    assert result.status == "PASS"
    assert result.details["dump_sha256"]
    missing = rehearsal.run_restore_rehearsal({})
    assert missing["status"] == "NOT_RUN"
    configuration = {
        "postgres": {"source_url": source, "restore_url": target},
        "object_store": {
            "source": {"bucket": "s3://production"},
            "destination": {"bucket": "s3://computeweaver-restore-release-one"},
            "source_prefix": "tenants/t/artifacts",
            "destination_prefix": "rehearsals/release-one",
        },
    }
    rehearsal.validate_restore_contract(
        configuration,
        postgres_contract={
            "source_database": "computeweaver",
            "restore_database": "computeweaver_restore_release_one",
        },
        object_contract={
            "source_bucket": "production",
            "restore_bucket": "computeweaver-restore-release-one",
            "source_prefix": "tenants/t/artifacts",
            "destination_prefix": "rehearsals/release-one",
        },
    )
    with pytest.raises(ValueError, match="approved recovery target"):
        rehearsal.validate_restore_contract(
            configuration,
            postgres_contract={"source_database": "wrong", "restore_database": "wrong"},
            object_contract={},
        )
    objective_report = rehearsal.enforce_recovery_objectives(
        {
            "checks": [
                {
                    "name": "postgres_restore",
                    "status": "PASS",
                    "started_at": datetime.now(UTC),
                    "finished_at": datetime.now(UTC) + timedelta(seconds=2),
                    "details": {},
                }
            ]
        },
        postgres_contract={"maximum_duration_seconds": 1},
        object_contract={"maximum_duration_seconds": 1},
    )
    assert objective_report["status"] == "FAIL"


def test_object_restore_target_validation() -> None:
    source = cast(S3ObjectStore, SimpleNamespace(bucket="production"))
    restore = cast(S3ObjectStore, SimpleNamespace(bucket="computeweaver-restore-release-one"))
    rehearsal.validate_object_targets(source, restore, "rehearsals/release-one/")
    with pytest.raises(ValueError, match="must differ"):
        rehearsal.validate_object_targets(
            source, cast(S3ObjectStore, SimpleNamespace(bucket="production")), "rehearsals/r/"
        )
    with pytest.raises(ValueError, match="under rehearsals"):
        rehearsal.validate_object_targets(source, restore, "unsafe/")


def test_object_restore_rehearsal_copies_versions_and_verifies_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Body:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def read(self, length: int | None = None) -> bytes:
            return self.content if length is None else self.content[:length]

        def close(self) -> None:
            pass

        def release_conn(self) -> None:
            pass

    class Client:
        def __init__(self, *, source: bool) -> None:
            self.objects: dict[str, bytes] = {"tenants/t/artifacts/one.json": b'{"safe":true}'} if source else {}

        def bucket_exists(self, _bucket: str) -> bool:
            return True

        def get_bucket_versioning(self, _bucket: str) -> SimpleNamespace:
            return SimpleNamespace(status="Enabled")

        def list_objects(self, _bucket: str, **_kwargs: object) -> list[SimpleNamespace]:
            return [SimpleNamespace(object_name=key, version_id="source-v1") for key in self.objects]

        def stat_object(self, bucket: str, key: str) -> None:
            if key not in self.objects:
                raise S3Error(
                    None,  # type: ignore[arg-type]
                    "NoSuchKey",
                    "missing",
                    key,
                    "request",
                    "host",
                    bucket,
                    key,
                )

        def get_object(self, _bucket: str, key: str, **_kwargs: object) -> Body:
            return Body(self.objects[key])

        def put_object(self, _bucket: str, key: str, body: Body, length: int, **_kwargs: object) -> SimpleNamespace:
            self.objects[key] = body.read(length)
            return SimpleNamespace(version_id="restore-v1")

    source_store = cast(S3ObjectStore, SimpleNamespace(bucket="production", client=Client(source=True)))
    restore_store = cast(
        S3ObjectStore,
        SimpleNamespace(bucket="computeweaver-restore-release-one", client=Client(source=False)),
    )
    stores = iter([source_store, restore_store])
    monkeypatch.setattr(rehearsal, "_store", lambda config, resolver: next(stores))
    result = rehearsal.rehearse_object_restore(
        {
            "source": {},
            "destination": {},
            "source_prefix": "tenants/t/artifacts",
            "destination_prefix": "rehearsals/release-one",
            "max_objects": 10,
        },
        resolver=CredentialResolver(Path("/nonexistent")),
    )
    assert result.status == "PASS"
    assert result.details["objects"][0]["source_version_id"] == "source-v1"
    assert result.details["objects"][0]["restore_version_id"] == "restore-v1"


def test_external_manifest_loader_requires_object(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        external.load_manifest(path)


def test_release_evaluator_rejects_summary_only_forged_attestations(tmp_path: Path) -> None:
    release_id = "release-one"
    revision = "abc123def456"
    for batch in range(1, 21):
        directory = tmp_path / f"B{batch:02d}"
        directory.mkdir()
        (directory / "manifest.json").write_text(
            json.dumps(
                {
                    "batch": f"B{batch:02d}",
                    "git_commit": revision,
                    "mandatory_gates": {
                        "tests": "PASS",
                        "contracts": "PASS",
                        "scenario": "PASS" if batch >= 9 else "NOT_APPLICABLE",
                    },
                }
            ),
            encoding="utf-8",
        )
    write_evidence(
        tmp_path / "B01" / "container-images.json",
        {
            "status": "PASS",
            "source_revision": revision,
            "container_build": {"status": "PASS"},
            "runtime_smoke": {"status": "PASS"},
        },
        command="container build",
        suite_name="hardened-container-build",
    )
    (tmp_path / "B01" / "build.log").write_text("verified evidence\n", encoding="utf-8")
    write_evidence(
        tmp_path / "B01" / "source-binding.json",
        {
            "status": "PASS",
            "source_revision": revision,
            "commit": revision,
            "tree": "a" * 40,
            "clean": True,
        },
        command="git source binding verification",
        suite_name="immutable-source-binding",
    )
    junit = '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"/></testsuites>'
    (tmp_path / "test-results.xml").write_text(junit, encoding="utf-8")
    (tmp_path / "postgres-integration.xml").write_text(junit, encoding="utf-8")
    (tmp_path / "coverage.xml").write_text('<coverage line-rate="0.90"/>', encoding="utf-8")
    write_evidence(
        tmp_path / "test-run-binding.json",
        {
            "status": "PASS",
            "source_revision": revision,
            "tree": "a" * 40,
            "clean": True,
            "junit_sha256": hashlib.sha256((tmp_path / "test-results.xml").read_bytes()).hexdigest(),
            "coverage_sha256": hashlib.sha256((tmp_path / "coverage.xml").read_bytes()).hexdigest(),
        },
        command="record unit tests",
        suite_name="unit-tests",
    )
    write_evidence(
        tmp_path / "postgres-integration-binding.json",
        {
            "status": "PASS",
            "source_revision": revision,
            "tree": "a" * 40,
            "clean": True,
            "junit_sha256": hashlib.sha256((tmp_path / "postgres-integration.xml").read_bytes()).hexdigest(),
        },
        command="record integration tests",
        suite_name="integration-tests",
    )
    (tmp_path / "B02" / "schema-catalog.json").write_text(
        json.dumps({"source_revision": revision, "openapi": {"path_count": 100}}),
        encoding="utf-8",
    )
    (tmp_path / "B10" / "batch-run-summary.json").write_text(
        json.dumps(
            {
                "passed": True,
                "scenarios": [
                    {
                        "name": f"scenario-{index}",
                        "evaluation": {"hard_violations": 0, "sla_violations": 0},
                    }
                    for index in range(10)
                ],
            }
        ),
        encoding="utf-8",
    )
    episodes = tmp_path / "B20" / "e2e-results" / "episode-summary.json"
    episodes.parent.mkdir(parents=True)
    episodes.write_text(
        json.dumps(
            {
                "fault_episodes": 100,
                "normal_episodes": 50,
                "hard_violations": 0,
                "sla_violations": 0,
            }
        ),
        encoding="utf-8",
    )
    request = create_evidence_request(
        release_id=release_id,
        source_revision=revision,
        requested_by="requester-one",
        requirements={name: {"required": True} for name in REQUIRED_EXTERNAL_GATES},
        nonce="release-evaluator-test",
    )
    write_request(tmp_path / "B20" / "evidence-request.json", request, command="certify request")
    common = {
        "status": "PASS",
        "release_id": release_id,
        "source_revision": revision,
        "request_sha256": request.request_sha256,
    }
    performance_path = tmp_path / "B20" / "production-performance-report.json"
    write_evidence(
        performance_path,
        {**common, "production_evidence": True},
        command="production load",
        suite_name="load",
    )
    write_evidence(
        tmp_path / "B20" / "restore-rehearsal.json",
        common,
        command="restore",
        suite_name="restore",
    )
    write_evidence(
        tmp_path / "B20" / "external-integrations.json",
        common,
        command="external",
        suite_name="external",
    )
    write_evidence(
        tmp_path / "B20" / "signed-attestations.json",
        {
            **common,
            "cryptographic_verification": True,
            "trust_policy_sha256": "b" * 64,
            "results": [
                {
                    "kind": "penetration_test",
                    "status": "PASS",
                    "expires_at": datetime.now(UTC) + timedelta(hours=1),
                },
                {
                    "kind": "human_acceptance",
                    "status": "PASS",
                    "expires_at": datetime.now(UTC) + timedelta(hours=1),
                },
            ],
        },
        command="verify attestations",
        suite_name="attestations",
    )
    gates = evaluate_production_evidence(tmp_path, release_id=release_id, source_revision=revision)
    assert next(gate for gate in gates if gate.name == "security").passed is False
    assert next(gate for gate in gates if gate.name == "acceptance").passed is False
    assert all(gate.passed for gate in gates if gate.name not in {"security", "acceptance"})
    binding_path = tmp_path / "test-run-binding.json"
    binding_path.write_text(binding_path.read_text(encoding="utf-8").replace(revision, "b" * 40))
    stale_tests = evaluate_production_evidence(tmp_path, release_id=release_id, source_revision=revision)
    assert next(gate for gate in stale_tests if gate.name == "tests").passed is False
    write_evidence(
        binding_path,
        {
            "status": "PASS",
            "source_revision": revision,
            "tree": "a" * 40,
            "clean": True,
            "junit_sha256": hashlib.sha256((tmp_path / "test-results.xml").read_bytes()).hexdigest(),
            "coverage_sha256": hashlib.sha256((tmp_path / "coverage.xml").read_bytes()).hexdigest(),
        },
        command="record unit tests",
        suite_name="unit-tests",
    )
    source_path = tmp_path / "B01" / "source-binding.json"
    source_path.write_text(source_path.read_text(encoding="utf-8").replace('"clean":true', '"clean":false'))
    source_tampered = evaluate_production_evidence(tmp_path, release_id=release_id, source_revision=revision)
    assert next(gate for gate in source_tampered if gate.name == "build").passed is False
    write_evidence(
        source_path,
        {
            "status": "PASS",
            "source_revision": revision,
            "commit": revision,
            "tree": "a" * 40,
            "clean": True,
        },
        command="git source binding verification",
        suite_name="immutable-source-binding",
    )
    unsealed = certify_release(
        release_id=release_id,
        commit=revision,
        generated_at=datetime.now(UTC),
        gate_results=gates,
    )
    assert unsealed.status == "NOT_CERTIFIED"
    certificate = certify_release(
        release_id=release_id,
        commit=revision,
        generated_at=datetime.now(UTC),
        gate_results=gates,
        evidence_hashes=collect_gate_evidence_hashes(tmp_path, gates),
    )
    assert certificate.status == "NOT_CERTIFIED"
    performance_path.write_text(
        performance_path.read_text(encoding="utf-8").replace(
            '"production_evidence":true', '"production_evidence":false'
        ),
        encoding="utf-8",
    )
    tampered = evaluate_production_evidence(tmp_path, release_id=release_id, source_revision=revision)
    assert next(gate for gate in tampered if gate.name == "performance").passed is False
    unversioned = evaluate_production_evidence(
        tmp_path, release_id=release_id, source_revision="UNVERSIONED-SOURCE-deadbeef"
    )
    assert next(gate for gate in unversioned if gate.name == "build").passed is False
    manifest_path = tmp_path / "B05" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["git_commit"] = "different-commit"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    unbound = evaluate_production_evidence(
        tmp_path,
        release_id=release_id,
        source_revision=revision,
    )
    assert all(
        next(gate for gate in unbound if gate.name == name).passed is False
        for name in ("tests", "contracts", "scenarios")
    )


def test_production_gate_jobs_are_suspended_digest_pinned_and_hardened() -> None:
    root = Path(__file__).resolve().parents[1]
    documents = list(yaml.safe_load_all((root / "deploy/kubernetes/production-gates.yaml").read_text(encoding="utf-8")))
    jobs = [document for document in documents if document.get("kind") == "Job"]
    assert {job["metadata"]["labels"]["gate"] for job in jobs} == {
        "external-acceptance",
        "production-load",
        "restore-rehearsal",
        "signed-attestations",
    }
    for job in jobs:
        assert job["spec"]["suspend"] is True
        assert job["spec"]["backoffLimit"] == 0
        pod = job["spec"]["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False
        assert pod["securityContext"]["runAsNonRoot"] is True
        container = pod["containers"][0]
        assert container["image"].startswith("computeweaver@sha256:")
        assert container["securityContext"] == {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        }
    by_gate = {job["metadata"]["labels"]["gate"]: job for job in jobs}
    external_args = by_gate["external-acceptance"]["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--token-ref" not in external_args
    load_args = by_gate["production-load"]["spec"]["template"]["spec"]["containers"][0]["args"]
    assert load_args[load_args.index("--token-ref") + 1] == "$(LOAD_TOKEN_REF)"
    assert "--policy" not in load_args
    attestation_args = by_gate["signed-attestations"]["spec"]["template"]["spec"]["containers"][0]["args"]
    assert attestation_args[attestation_args.index("--policy") + 1] == (
        "/gate-config/production-attestation-trust-policy.json"
    )
