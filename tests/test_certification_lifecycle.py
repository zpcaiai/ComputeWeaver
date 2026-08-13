from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from apps.api import main as api_main
from packages.certification import cli as certification_cli
from packages.certification.lifecycle import CertificationRepository
from packages.certification.requests import REQUIRED_EXTERNAL_GATES
from packages.certification.service import (
    CertificationResult,
    GateResult,
    certificate_from_document,
    certify_release,
)
from packages.certification.signing import (
    attach_release_signature,
    issue_release_token,
    verify_release_token,
)

client = TestClient(api_main.app)
ADMIN_HEADERS = {
    "X-Tenant-Id": "tenant-certification",
    "X-Actor-Id": "release-operator",
    "X-Roles": "admin,safety_admin",
    "Idempotency-Key": "certification-api-0001",
}


def _certified_result() -> tuple[CertificationResult, dict[str, str]]:
    evidence = {f"evidence/{name}.json": hashlib.sha256(name.encode()).hexdigest() for name in "abcdefghi"}
    gates = tuple(
        GateResult(name, True, (reference,))
        for name, reference in zip(
            (
                "build",
                "tests",
                "contracts",
                "security",
                "scenarios",
                "performance",
                "backup_restore",
                "external_integrations",
                "acceptance",
            ),
            evidence,
            strict=True,
        )
    )
    return (
        certify_release(
            release_id="release-one",
            commit="abc123def456",
            generated_at=datetime.now(UTC),
            gate_results=gates,
            evidence_hashes=evidence,
            artifacts=evidence,
            test_summary={"tests": 144, "failures": 0},
            scenario_metrics={"fault_episodes": 100, "normal_episodes": 50},
            approvals=(
                {"role": "product_owner", "actor": "product-one", "decision": "ACCEPT"},
                {"role": "security_owner", "actor": "security-one", "decision": "ACCEPT"},
                {"role": "operations_owner", "actor": "operations-one", "decision": "ACCEPT"},
            ),
        ),
        evidence,
    )


def test_signed_certificate_lifecycle_persists_events_and_revocation(tmp_path: Path) -> None:
    raw_result, _ = _certified_result()
    result = raw_result
    assert result.status == "CERTIFIED"
    key = ec.generate_private_key(ec.SECP256R1())
    private_path = tmp_path / "release-private.pem"
    public_path = tmp_path / "release-public.pem"
    private_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(private_path, 0o600)
    public_path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    token = issue_release_token(result, private_key_file=private_path, key_id="release-key-one")
    signed = attach_release_signature(result, token)
    claims = verify_release_token(token, public_key_file=public_path, expected_certificate=signed)
    assert claims["certificate_hash"] == signed.certificate_hash

    repository = CertificationRepository(tmp_path / "evidence")
    repository.save_run(result, actor_id="release-operator")
    repository.publish(
        signed,
        actor_id="release-operator",
        public_key_file=public_path,
    )
    view = repository.view("release-one")
    assert view["status"] == "CERTIFIED"
    assert view["signature"] == token
    assert certificate_from_document(json.loads(json.dumps(asdict(signed), default=str))) == signed
    assert [event.event_type for event in repository.events("release-one")] == [
        "CertificationStarted",
        "ReleaseCertified",
    ]
    assert repository.verify_event_chain("release-one")

    revoked = repository.revoke(
        "release-one",
        actor_id="security-admin",
        reason="independent incident response decision",
    )
    assert revoked["status"] == "REVOKED"
    assert repository.view("release-one")["status"] == "REVOKED"
    assert repository.verify_event_chain("release-one")
    registry = json.loads((tmp_path / "evidence/B20/revocations.json").read_text())
    assert registry["revocations"][0]["certificate_hash"] == signed.certificate_hash


def test_failed_run_emits_each_gate_failure_without_publishing(tmp_path: Path) -> None:
    evidence = {"evidence/build.json": "a" * 64}
    result = certify_release(
        release_id="failed-release",
        commit="abc123def456",
        generated_at=datetime.now(UTC),
        gate_results=(GateResult("build", False, tuple(evidence), "build failed"),),
        evidence_hashes=evidence,
    )
    repository = CertificationRepository(tmp_path / "evidence")
    repository.save_run(result, actor_id="release-operator")
    event_types = [event.event_type for event in repository.events("failed-release")]
    assert event_types[0] == "CertificationStarted"
    assert event_types.count("GateFailed") == 9
    assert repository.view("failed-release")["status"] == "NOT_CERTIFIED"
    with pytest.raises(ValueError, match="only a published certificate"):
        repository.revoke(
            "failed-release",
            actor_id="security-admin",
            reason="invalid release must not be revocable",
        )


def test_certification_api_runs_publishes_and_exposes_integrity_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = _certified_result()
    key = ec.generate_private_key(ec.SECP256R1())
    private_path = tmp_path / "release-private.pem"
    public_path = tmp_path / "release-public.pem"
    private_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(private_path, 0o600)
    public_path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    repository = CertificationRepository(tmp_path / "evidence")
    monkeypatch.setattr(api_main, "certification_repository", repository)
    monkeypatch.setattr(
        api_main,
        "settings",
        replace(
            api_main.settings,
            release_commit=result.commit,
            certification_evidence_root=str(tmp_path / "evidence"),
            release_signing_key_file=str(private_path),
            release_public_key_file=str(public_path),
            release_signing_key_id="release-key-one",
        ),
    )
    monkeypatch.setattr(api_main, "evaluate_release_from_evidence", lambda *_args, **_kwargs: result)

    run = client.post(
        f"/v1/certification/{result.release_id}/run",
        headers=ADMIN_HEADERS,
        json={"expected_source_revision": result.commit},
    )
    assert run.status_code == 200
    assert run.json()["status"] == "READY_FOR_RELEASE"
    events = client.get(f"/v1/certification/{result.release_id}/events", headers=ADMIN_HEADERS)
    assert events.status_code == 200
    assert events.json()["integrity"] == "PASS"
    assert [item["event_type"] for item in events.json()["events"]] == ["CertificationStarted"]

    published = client.post(
        f"/v1/certification/{result.release_id}/publish",
        headers={**ADMIN_HEADERS, "Idempotency-Key": "certification-api-0002"},
        json={
            "expected_source_revision": result.commit,
            "expected_certificate_hash": result.certificate_hash,
        },
    )
    assert published.status_code == 200
    assert published.json()["status"] == "CERTIFIED"
    assert published.json()["published"] is True
    assert published.json()["signature"]
    assert repository.verify_event_chain(result.release_id)


def test_certification_api_is_version_bound_role_separated_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = _certified_result()
    monkeypatch.setattr(api_main, "certification_repository", CertificationRepository(tmp_path / "evidence"))
    monkeypatch.setattr(
        api_main,
        "settings",
        replace(
            api_main.settings,
            release_commit=result.commit,
            certification_evidence_root=str(tmp_path / "evidence"),
            release_signing_key_file=None,
            release_public_key_file=None,
        ),
    )
    monkeypatch.setattr(api_main, "evaluate_release_from_evidence", lambda *_args, **_kwargs: result)
    viewer = {**ADMIN_HEADERS, "X-Roles": "viewer", "Idempotency-Key": "certification-role-0001"}
    unauthorized = client.post(
        f"/v1/certification/{result.release_id}/run",
        headers=viewer,
        json={"expected_source_revision": result.commit},
    )
    assert unauthorized.status_code == 403
    mismatch = client.post(
        f"/v1/certification/{result.release_id}/run",
        headers={**ADMIN_HEADERS, "Idempotency-Key": "certification-version-0001"},
        json={"expected_source_revision": "deadbeef"},
    )
    assert mismatch.status_code == 409

    run = client.post(
        f"/v1/certification/{result.release_id}/run",
        headers={**ADMIN_HEADERS, "Idempotency-Key": "certification-run-0003"},
        json={"expected_source_revision": result.commit},
    )
    assert run.status_code == 200
    publish = client.post(
        f"/v1/certification/{result.release_id}/publish",
        headers={**ADMIN_HEADERS, "Idempotency-Key": "certification-publish-0003"},
        json={
            "expected_source_revision": result.commit,
            "expected_certificate_hash": result.certificate_hash,
        },
    )
    assert publish.status_code == 422
    assert "signing" in publish.json()["message"]


def test_certification_api_creates_one_release_bound_external_evidence_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = _certified_result()
    evidence_root = tmp_path / "evidence"
    monkeypatch.setattr(api_main, "certification_repository", CertificationRepository(evidence_root))
    monkeypatch.setattr(
        api_main,
        "settings",
        replace(
            api_main.settings,
            release_commit=result.commit,
            certification_evidence_root=str(evidence_root),
        ),
    )
    request_body = {
        "expected_source_revision": result.commit,
        "valid_for_hours": 24,
        "requirements": {name: {"required": True} for name in REQUIRED_EXTERNAL_GATES},
    }
    created = client.post(
        f"/v1/certification/{result.release_id}/evidence-request",
        headers={**ADMIN_HEADERS, "Idempotency-Key": "evidence-request-api-001"},
        json=request_body,
    )
    assert created.status_code == 200
    assert created.json()["source_revision"] == result.commit
    assert created.json()["requested_by"] == "release-operator"
    assert (evidence_root / "B20/evidence-request.json.sha256").is_file()
    duplicate = client.post(
        f"/v1/certification/{result.release_id}/evidence-request",
        headers={**ADMIN_HEADERS, "Idempotency-Key": "evidence-request-api-002"},
        json=request_body,
    )
    assert duplicate.status_code == 409


def test_repository_rejects_publish_bypass_wrong_key_and_recertification(tmp_path: Path) -> None:
    result, _ = _certified_result()
    signing_key = ec.generate_private_key(ec.SECP256R1())
    wrong_key = ec.generate_private_key(ec.SECP256R1())
    private_path = tmp_path / "release-private.pem"
    public_path = tmp_path / "release-public.pem"
    wrong_public_path = tmp_path / "wrong-release-public.pem"
    private_path.write_bytes(
        signing_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(private_path, 0o600)
    public_path.write_bytes(
        signing_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    wrong_public_path.write_bytes(
        wrong_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    signed = attach_release_signature(
        result,
        issue_release_token(result, private_key_file=private_path),
    )
    repository = CertificationRepository(tmp_path / "evidence")
    with pytest.raises(ValueError, match="integrity check failed"):
        repository.publish(
            signed,
            actor_id="release-operator",
            public_key_file=public_path,
        )
    repository.save_run(result, actor_id="release-operator")
    assert repository.view(result.release_id)["status"] == "READY_FOR_RELEASE"
    assert repository.view(result.release_id)["published"] is False
    with pytest.raises(jwt.InvalidSignatureError):
        repository.publish(
            signed,
            actor_id="release-operator",
            public_key_file=wrong_public_path,
        )
    repository.publish(
        signed,
        actor_id="release-operator",
        public_key_file=public_path,
    )
    with pytest.raises(FileExistsError, match="cannot be recertified"):
        repository.save_run(result, actor_id="release-operator")
    repository.revoke(
        result.release_id,
        actor_id="security-admin",
        reason="confirmed compromise",
    )
    with pytest.raises(ValueError, match="cannot be republished"):
        repository.publish(
            signed,
            actor_id="release-operator",
            public_key_file=public_path,
        )


def test_certificate_document_rejects_invalid_hashes_and_gate_shape() -> None:
    result, _ = _certified_result()
    document = json.loads(json.dumps(asdict(result), default=str))
    document["gates"].pop()
    with pytest.raises(ValueError, match="mandatory gate set"):
        certificate_from_document(document)
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        certify_release(
            release_id="invalid-hash-release",
            commit="abc123def456",
            generated_at=datetime.now(UTC),
            gate_results=(GateResult("build", True, ("evidence/build.json",)),),
            evidence_hashes={"evidence/build.json": "not-a-sha256"},
        )


def test_cli_report_release_and_revoke_are_distinct_lifecycle_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, _ = _certified_result()
    evidence_root = tmp_path / "evidence"
    repository = CertificationRepository(evidence_root)
    repository.save_run(result, actor_id="release-operator")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "certify",
            "report",
            "--evidence",
            str(evidence_root),
            "--release-id",
            result.release_id,
        ],
    )
    certification_cli.main()
    assert json.loads(capsys.readouterr().out)["status"] == "READY_FOR_RELEASE"

    key = ec.generate_private_key(ec.SECP256R1())
    private_path = tmp_path / "release-private.pem"
    public_path = tmp_path / "release-public.pem"
    private_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(private_path, 0o600)
    public_path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    monkeypatch.setattr(certification_cli, "_evaluate", lambda *args, **kwargs: result)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "certify",
            "release",
            "--evidence",
            str(evidence_root),
            "--release-id",
            result.release_id,
            "--commit",
            result.commit,
            "--actor",
            "release-operator",
            "--signing-key",
            str(private_path),
            "--verification-key",
            str(public_path),
        ],
    )
    certification_cli.main()
    assert json.loads(capsys.readouterr().out)["signature"]

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "certify",
            "revoke",
            "--evidence",
            str(evidence_root),
            "--release-id",
            result.release_id,
            "--actor",
            "security-admin",
            "--reason",
            "confirmed production security incident",
        ],
    )
    certification_cli.main()
    assert json.loads(capsys.readouterr().out)["status"] == "REVOKED"


def test_certification_api_reads_persisted_release_and_audits_revocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = _certified_result()
    key = ec.generate_private_key(ec.SECP256R1())
    private_path = tmp_path / "release-private.pem"
    public_path = tmp_path / "release-public.pem"
    private_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(private_path, 0o600)
    public_path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    signed = attach_release_signature(
        result,
        issue_release_token(result, private_key_file=private_path),
    )
    repository = CertificationRepository(tmp_path / "evidence")
    repository.save_run(result, actor_id="release-operator")
    repository.publish(
        signed,
        actor_id="release-operator",
        public_key_file=public_path,
    )
    monkeypatch.setattr(api_main, "certification_repository", repository)
    headers = {
        "X-Tenant-Id": "tenant-certification-api",
        "X-Actor-Id": "security-admin",
        "X-Roles": "admin",
        "Idempotency-Key": "certification-revoke-0001",
    }
    response = client.get("/v1/certification/release-one", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "CERTIFIED"
    revoked = client.post(
        "/v1/certification/release-one/revoke",
        headers=headers,
        json={"reason": "confirmed production security incident"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "REVOKED"
    assert client.get("/v1/certification/release-one", headers=headers).json()["status"] == "REVOKED"
