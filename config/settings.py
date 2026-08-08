from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def _tls_setting(value: str) -> str | bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str = "development"
    database_url: str = field(
        default="postgresql://computeweaver:change-me@127.0.0.1:5432/computeweaver",
        repr=False,
    )
    object_store: str = "s3://computeweaver"
    object_store_endpoint: str | None = None
    object_store_access_key: str | None = None
    object_store_secret_key: str | None = field(default=None, repr=False)
    object_store_ca_bundle: str | None = None
    log_level: str = "INFO"
    auth_mode: str = "oidc"
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_jwks_ttl_seconds: int = 300
    trusted_proxy_secret: str | None = field(default=None, repr=False)
    database_pool_min: int = 1
    database_pool_max: int = 10
    database_connect_timeout_seconds: int = 5
    migration_on_startup: bool = False
    otlp_endpoint: str | None = None
    service_name: str = "computeweaver-api"
    executor_target: str | None = None
    executor_url: str | None = None
    executor_token: str | None = field(default=None, repr=False)
    executor_ca_bundle: str | bool = True
    executor_client_certificate: str | None = None
    executor_client_key: str | None = None
    release_public_key_file: str | None = None
    release_revocations_file: str | None = None
    release_commit: str | None = None
    external_write_enabled: bool = False
    execution_mode: str = "read_only"
    release_certificate: str | None = field(default=None, repr=False)
    certification_evidence_root: str = "evidence"

    @classmethod
    def from_env(cls) -> Settings:
        raw_write = os.getenv("COMPUTEWEAVER_EXTERNAL_WRITE_ENABLED", "false").lower()
        return cls(
            environment=os.getenv("COMPUTEWEAVER_ENV", "development"),
            database_url=os.getenv(
                "COMPUTEWEAVER_DATABASE_URL",
                "postgresql://computeweaver:change-me@127.0.0.1:5432/computeweaver",
            ),
            object_store=os.getenv("COMPUTEWEAVER_OBJECT_STORE", "s3://computeweaver"),
            object_store_endpoint=os.getenv("COMPUTEWEAVER_OBJECT_STORE_ENDPOINT") or None,
            object_store_access_key=os.getenv("COMPUTEWEAVER_OBJECT_STORE_ACCESS_KEY") or None,
            object_store_secret_key=os.getenv("COMPUTEWEAVER_OBJECT_STORE_SECRET_KEY") or None,
            object_store_ca_bundle=os.getenv("COMPUTEWEAVER_OBJECT_STORE_CA_BUNDLE") or None,
            log_level=os.getenv("COMPUTEWEAVER_LOG_LEVEL", "INFO"),
            auth_mode=os.getenv("COMPUTEWEAVER_AUTH_MODE", "oidc"),
            oidc_issuer=os.getenv("COMPUTEWEAVER_OIDC_ISSUER") or None,
            oidc_audience=os.getenv("COMPUTEWEAVER_OIDC_AUDIENCE") or None,
            oidc_jwks_url=os.getenv("COMPUTEWEAVER_OIDC_JWKS_URL") or None,
            oidc_jwks_ttl_seconds=int(os.getenv("COMPUTEWEAVER_OIDC_JWKS_TTL_SECONDS", "300")),
            trusted_proxy_secret=os.getenv("COMPUTEWEAVER_TRUSTED_PROXY_SECRET") or None,
            database_pool_min=int(os.getenv("COMPUTEWEAVER_DATABASE_POOL_MIN", "1")),
            database_pool_max=int(os.getenv("COMPUTEWEAVER_DATABASE_POOL_MAX", "10")),
            database_connect_timeout_seconds=int(
                os.getenv("COMPUTEWEAVER_DATABASE_CONNECT_TIMEOUT_SECONDS", "5")
            ),
            migration_on_startup=os.getenv("COMPUTEWEAVER_MIGRATION_ON_STARTUP", "false").lower()
            == "true",
            otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
            service_name=os.getenv("OTEL_SERVICE_NAME", "computeweaver-api"),
            executor_target=os.getenv("COMPUTEWEAVER_EXECUTOR_TARGET") or None,
            executor_url=os.getenv("COMPUTEWEAVER_EXECUTOR_URL") or None,
            executor_token=os.getenv("COMPUTEWEAVER_EXECUTOR_TOKEN") or None,
            executor_ca_bundle=_tls_setting(os.getenv("COMPUTEWEAVER_EXECUTOR_CA_BUNDLE", "true")),
            executor_client_certificate=os.getenv("COMPUTEWEAVER_EXECUTOR_CLIENT_CERT") or None,
            executor_client_key=os.getenv("COMPUTEWEAVER_EXECUTOR_CLIENT_KEY") or None,
            release_public_key_file=os.getenv("COMPUTEWEAVER_RELEASE_PUBLIC_KEY_FILE") or None,
            release_revocations_file=os.getenv("COMPUTEWEAVER_RELEASE_REVOCATIONS_FILE") or None,
            release_commit=os.getenv("COMPUTEWEAVER_RELEASE_COMMIT") or None,
            external_write_enabled=raw_write == "true",
            execution_mode=os.getenv("COMPUTEWEAVER_EXECUTION_MODE", "read_only"),
            release_certificate=os.getenv("COMPUTEWEAVER_RELEASE_CERTIFICATE") or None,
            certification_evidence_root=os.getenv("COMPUTEWEAVER_CERTIFICATION_EVIDENCE_ROOT", "evidence"),
        )

    @property
    def in_memory_mode(self) -> bool:
        return self.database_url == "memory://"

    def validate(self) -> None:
        if self.environment not in {"development", "test", "simulator", "production"}:
            raise ValueError("unknown ComputeWeaver environment")
        if self.execution_mode not in {"read_only", "guarded"}:
            raise ValueError("execution mode must be read_only or guarded")
        if self.database_pool_min < 1 or self.database_pool_max < self.database_pool_min:
            raise ValueError("invalid database pool bounds")
        if self.oidc_jwks_ttl_seconds < 1:
            raise ValueError("OIDC JWKS TTL must be positive")
        if self.auth_mode not in {"oidc", "trusted_headers"}:
            raise ValueError("COMPUTEWEAVER_AUTH_MODE must be oidc or trusted_headers")
        if self.auth_mode == "trusted_headers" and self.environment not in {"test", "simulator"}:
            raise ValueError("trusted header authentication is restricted to test/simulator")
        if self.in_memory_mode and self.environment not in {"test", "simulator"}:
            raise ValueError("memory persistence is restricted to test/simulator")
        if self.auth_mode == "oidc" and not all(
            (self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)
        ):
            raise ValueError("OIDC issuer, audience, and JWKS URL are required")
        if self.environment == "production":
            if not self.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
                raise ValueError("production requires PostgreSQL")
            database = urlparse(self.database_url.replace("postgresql+psycopg://", "postgresql://", 1))
            ssl_mode = parse_qs(database.query).get("sslmode", [""])[0]
            if ssl_mode not in {"require", "verify-ca", "verify-full"}:
                raise ValueError("production PostgreSQL requires TLS sslmode")
            if "change-me" in self.database_url:
                raise ValueError("production database credentials cannot use placeholder values")
            if self.object_store.startswith("file://"):
                raise ValueError("production object storage cannot use a local filesystem")
            if not self.object_store.startswith("s3://") or not all(
                (
                    self.object_store_endpoint,
                    self.object_store_access_key,
                    self.object_store_secret_key,
                )
            ):
                raise ValueError("production requires configured S3-compatible object storage")
            if not str(self.object_store_endpoint).startswith("https://"):
                raise ValueError("production object storage endpoint must use HTTPS")
            if self.object_store_secret_key == "change-me":  # noqa: S105 - reject known placeholder
                raise ValueError("production object storage cannot use placeholder credentials")
            if not str(self.oidc_issuer).startswith("https://") or not str(self.oidc_jwks_url).startswith(
                "https://"
            ):
                raise ValueError("production OIDC issuer and JWKS URL must use HTTPS")
            if self.otlp_endpoint and not self.otlp_endpoint.startswith("https://"):
                raise ValueError("production OTLP endpoint must use HTTPS")
            if self.external_write_enabled:
                if not self.executor_target or not str(self.executor_url).startswith("https://"):
                    raise ValueError("guarded execution requires a fixed HTTPS provider gateway")
                if bool(self.executor_client_certificate) != bool(self.executor_client_key):
                    raise ValueError("executor mTLS certificate and key must be configured together")
                if self.executor_ca_bundle is False:
                    raise ValueError("guarded execution cannot disable TLS verification")
                if not self.external_writes_allowed():
                    raise ValueError(
                        "guarded execution requires a release token, public key, revocation registry, and commit"
                    )
                required_files = (
                    self.release_public_key_file,
                    self.release_revocations_file,
                    self.executor_client_certificate,
                    self.executor_client_key,
                )
                if any(value and not Path(value).is_file() for value in required_files):
                    raise ValueError("guarded execution key or certificate file is unavailable")

    def external_writes_allowed(self) -> bool:
        return bool(
            self.external_write_enabled
            and self.execution_mode == "guarded"
            and self.release_certificate
            and self.release_public_key_file
            and self.release_revocations_file
            and self.release_commit
            and self.environment == "production"
        )
