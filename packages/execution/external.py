from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import jwt

from config.settings import Settings
from packages.certification.evidence import verify_evidence
from packages.certification.signing import verify_release_token


class ReleaseGate:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def validate(self) -> dict[str, Any]:
        if not self.settings.external_writes_allowed():
            raise PermissionError("external execution feature gate is closed")
        key_file = Path(self.settings.release_public_key_file or "")
        if not key_file.is_file():
            raise PermissionError("release certification public key is unavailable")
        revocations_file = Path(self.settings.release_revocations_file or "")
        integrity, integrity_error = verify_evidence(revocations_file)
        if not integrity:
            raise PermissionError(f"release revocation registry is unavailable or invalid: {integrity_error}")
        try:
            token = self.settings.release_certificate
            if token is None:
                raise PermissionError("release certification token is unavailable")
            claims = verify_release_token(
                token,
                public_key_file=key_file,
            )
        except (jwt.PyJWTError, OSError, ValueError) as error:
            raise PermissionError("release certification token is invalid") from error
        if claims.get("status") != "CERTIFIED":
            raise PermissionError("release is not certified")
        if claims.get("commit") != self.settings.release_commit:
            raise PermissionError("release certificate does not match running commit")
        try:
            revocations = json.loads(revocations_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PermissionError("release revocation registry cannot be read") from error
        if any(
            isinstance(entry, dict) and entry.get("certificate_hash") == claims.get("certificate_hash")
            for entry in revocations.get("revocations", [])
        ):
            raise PermissionError("release certificate has been revoked")
        return claims


class GuardedHttpExecutor:
    """High-level provider gateway; low-level breaker/relay/firmware commands never reach it."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.release_gate = ReleaseGate(settings)

    def _client(self) -> httpx.Client:
        if not self.settings.executor_url or not self.settings.executor_url.startswith("https://"):
            raise PermissionError("guarded executor HTTPS URL is not configured")
        headers = {"Accept": "application/json"}
        if self.settings.executor_token:
            headers["Authorization"] = f"Bearer {self.settings.executor_token}"
        certificate: str | tuple[str, str] | None = None
        if self.settings.executor_client_certificate and self.settings.executor_client_key:
            certificate = (
                self.settings.executor_client_certificate,
                self.settings.executor_client_key,
            )
        return httpx.Client(
            base_url=self.settings.executor_url,
            headers=headers,
            timeout=15,
            verify=self.settings.executor_ca_bundle,
            cert=certificate,
            follow_redirects=False,
        )

    def _check_target(self, target: str) -> None:
        if not self.settings.executor_target or target != self.settings.executor_target:
            raise PermissionError("action target is not the configured provider gateway")

    def dry_run(self, target: str, kind: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self._check_target(target)
        with self._client() as client:
            response = client.post(
                "/v1/actions/dry-run",
                json={"kind": kind, "parameters": parameters},
            )
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict) or result.get("valid") is not True:
            raise ValueError("provider dry-run did not validate the action")
        return dict(result)

    def execute(
        self,
        target: str,
        kind: str,
        parameters: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._check_target(target)
        certificate = self.release_gate.validate()
        with self._client() as client:
            response = client.post(
                "/v1/actions",
                headers={
                    "Idempotency-Key": idempotency_key,
                    "X-ComputeWeaver-Release": str(certificate["release_id"]),
                },
                json={"kind": kind, "parameters": parameters},
            )
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict) or result.get("status") not in {"accepted", "executed"}:
            raise RuntimeError("provider did not acknowledge action execution")
        return dict(result)
