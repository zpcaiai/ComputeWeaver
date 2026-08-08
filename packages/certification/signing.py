from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jwt

from .requests import IMMUTABLE_REVISION
from .service import CertificationResult, verify_certificate

ALGORITHMS = frozenset({"RS256", "ES256", "EdDSA"})


def issue_release_token(
    certificate: CertificationResult,
    *,
    private_key_file: Path,
    algorithm: str = "ES256",
    key_id: str | None = None,
    now: datetime | None = None,
) -> str:
    issued_at = now or datetime.now(UTC)
    if issued_at.tzinfo is None:
        raise ValueError("release token issue time must be timezone-aware")
    if algorithm not in ALGORITHMS:
        raise ValueError("unsupported release signing algorithm")
    if certificate.status != "CERTIFIED" or not verify_certificate(certificate):
        raise ValueError("only a verified CERTIFIED result can be signed")
    if certificate.expires_at <= issued_at:
        raise ValueError("expired release certificates cannot be signed")
    if not IMMUTABLE_REVISION.fullmatch(certificate.commit):
        raise ValueError("release tokens require an immutable source revision")
    mode = private_key_file.stat().st_mode & 0o777
    if mode & 0o077:
        raise PermissionError("release signing key must not be group/world accessible")
    artifact_set_hash = hashlib.sha256(json.dumps(certificate.artifacts, sort_keys=True).encode()).hexdigest()
    payload = {
        "iss": "computeweaver-certifier",
        "aud": "computeweaver-execution",
        "sub": certificate.release_id,
        "iat": issued_at,
        "nbf": issued_at,
        "exp": certificate.expires_at,
        "jti": certificate.certificate_hash,
        "status": certificate.status,
        "commit": certificate.commit,
        "release_id": certificate.release_id,
        "certificate_hash": certificate.certificate_hash,
        "artifact_set_hash": artifact_set_hash,
    }
    headers = {"kid": key_id} if key_id else None
    return jwt.encode(
        payload,
        private_key_file.read_bytes(),
        algorithm=algorithm,
        headers=headers,
    )


def attach_release_signature(
    certificate: CertificationResult,
    token: str,
) -> CertificationResult:
    return replace(certificate, signature=token)


def verify_release_token(
    token: str,
    *,
    public_key_file: Path,
    expected_certificate: CertificationResult | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    claims: dict[str, Any] = jwt.decode(
        token,
        public_key_file.read_bytes(),
        algorithms=sorted(ALGORITHMS),
        audience="computeweaver-execution",
        issuer="computeweaver-certifier",
        options={
            "require": [
                "exp",
                "iat",
                "nbf",
                "iss",
                "aud",
                "jti",
                "status",
                "commit",
                "release_id",
                "certificate_hash",
                "artifact_set_hash",
            ]
        },
        leeway=30,
    )
    if now and datetime.fromtimestamp(float(claims["exp"]), tz=UTC) <= now:
        raise ValueError("release token is expired")
    if claims.get("status") != "CERTIFIED":
        raise ValueError("release token is not certified")
    if claims.get("jti") != claims.get("certificate_hash"):
        raise ValueError("release token identifier does not match its certificate")
    if expected_certificate:
        if not verify_certificate(expected_certificate):
            raise ValueError("expected release certificate is invalid")
        expected = {
            "release_id": expected_certificate.release_id,
            "commit": expected_certificate.commit,
            "certificate_hash": expected_certificate.certificate_hash,
        }
        if any(claims.get(name) != value for name, value in expected.items()):
            raise ValueError("release token does not match the expected certificate")
        artifacts_hash = hashlib.sha256(json.dumps(expected_certificate.artifacts, sort_keys=True).encode()).hexdigest()
        if claims.get("artifact_set_hash") != artifacts_hash:
            raise ValueError("release token artifact set does not match the certificate")
    return claims
