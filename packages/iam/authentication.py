from __future__ import annotations

import hmac
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWK

from config.settings import Settings

from .service import Identity, validate_sso_claims


class AuthenticationError(PermissionError):
    pass


@dataclass(slots=True)
class _JwksEntry:
    expires_at: float
    keys: dict[str, PyJWK]


class JwksCache:
    def __init__(self, url: str, ttl_seconds: int, timeout_seconds: float = 5) -> None:
        if not url.startswith("https://"):
            raise ValueError("OIDC JWKS URL must use HTTPS")
        self.url = url
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = timeout_seconds
        self._entry: _JwksEntry | None = None
        self._lock = threading.RLock()

    def get(self, key_id: str) -> PyJWK:
        with self._lock:
            now = time.monotonic()
            if self._entry is None or now >= self._entry.expires_at or key_id not in self._entry.keys:
                self._entry = self._refresh(now)
            try:
                return self._entry.keys[key_id]
            except KeyError as error:
                raise AuthenticationError("JWT signing key is not trusted") from error

    def _refresh(self, now: float) -> _JwksEntry:
        try:
            response = httpx.get(self.url, timeout=self.timeout_seconds, follow_redirects=False)
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise AuthenticationError("OIDC JWKS endpoint is unavailable") from error
        raw_keys = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(raw_keys, list):
            raise AuthenticationError("OIDC JWKS document is invalid")
        keys: dict[str, PyJWK] = {}
        for raw in raw_keys:
            if not isinstance(raw, dict) or not isinstance(raw.get("kid"), str):
                continue
            parsed = PyJWK.from_dict(raw)
            if parsed.algorithm_name not in {"RS256", "RS384", "RS512", "ES256", "ES384"}:
                continue
            keys[str(raw["kid"])] = parsed
        if not keys:
            raise AuthenticationError("OIDC JWKS contains no allowed signing keys")
        return _JwksEntry(now + self.ttl_seconds, keys)


class Authenticator:
    ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384"})

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jwks = (
            JwksCache(settings.oidc_jwks_url, settings.oidc_jwks_ttl_seconds)
            if settings.auth_mode == "oidc" and settings.oidc_jwks_url
            else None
        )

    def authenticate(
        self,
        *,
        authorization: str | None,
        trusted_headers: dict[str, str | None],
        peer_certificate_sha256: str | None = None,
    ) -> Identity:
        if self.settings.auth_mode == "trusted_headers":
            return self._trusted_identity(trusted_headers)
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthenticationError("Bearer token is required")
        token = authorization.removeprefix("Bearer ").strip()
        if not token or token.count(".") != 2:
            raise AuthenticationError("Bearer token is malformed")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as error:
            raise AuthenticationError("Bearer token header is invalid") from error
        algorithm = header.get("alg")
        key_id = header.get("kid")
        if algorithm not in self.ALGORITHMS or not isinstance(key_id, str) or not self._jwks:
            raise AuthenticationError("Bearer token signing method is not trusted")
        key = self._jwks.get(key_id)
        if key.algorithm_name != algorithm:
            raise AuthenticationError("Bearer token algorithm does not match signing key")
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=[algorithm],
                audience=self.settings.oidc_audience,
                issuer=self.settings.oidc_issuer,
                options={"require": ["exp", "iss", "aud", "sub", "tenant_id", "roles"]},
                leeway=30,
            )
            identity = validate_sso_claims(
                claims,
                self.settings.oidc_issuer or "",
                self.settings.oidc_audience or "",
            )
        except (jwt.PyJWTError, PermissionError, ValueError) as error:
            raise AuthenticationError("Bearer token validation failed") from error
        if identity.service_account:
            bound_hash = claims.get("mtls_sha256")
            if not isinstance(bound_hash, str) or not peer_certificate_sha256:
                raise AuthenticationError("service account token requires mTLS certificate binding")
            if not hmac.compare_digest(bound_hash.lower(), peer_certificate_sha256.lower()):
                raise AuthenticationError("service account certificate binding mismatch")
        return identity

    def _trusted_identity(self, headers: dict[str, str | None]) -> Identity:
        from datetime import UTC, datetime, timedelta

        tenant_id = headers.get("tenant_id")
        actor_id = headers.get("actor_id")
        roles = headers.get("roles")
        if not tenant_id or not actor_id or roles is None:
            raise AuthenticationError("test identity headers are incomplete")
        return Identity(
            subject=actor_id,
            tenant_id=tenant_id,
            roles=frozenset(role.strip() for role in roles.split(",") if role.strip()),
            attributes={},
            token_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


def forwarded_peer_certificate(
    *,
    direct_hash: str | None,
    forwarded_hash: str | None,
    supplied_proxy_secret: str | None,
    configured_proxy_secret: str | None,
) -> str | None:
    if direct_hash:
        return direct_hash
    if not forwarded_hash or not supplied_proxy_secret or not configured_proxy_secret:
        return None
    if not hmac.compare_digest(supplied_proxy_secret, configured_proxy_secret):
        return None
    return forwarded_hash
