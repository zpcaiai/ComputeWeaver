from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import urllib3
from minio import Minio


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    bucket: str
    key: str
    etag: str
    version_id: str | None
    sha256: str
    size: int


class S3ObjectStore:
    """S3-compatible immutable-artifact store with tenant prefixing and digest checks."""

    def __init__(
        self,
        *,
        bucket_url: str,
        endpoint: str,
        access_key: str,
        secret_key: str,
        ca_bundle: str | None = None,
    ) -> None:
        bucket = urlparse(bucket_url)
        service = urlparse(endpoint)
        if bucket.scheme != "s3" or not bucket.netloc:
            raise ValueError("object store bucket must use s3://bucket")
        if service.scheme not in {"http", "https"} or not service.netloc:
            raise ValueError("object store endpoint must be an HTTP(S) URL")
        self.bucket = bucket.netloc
        secure = service.scheme == "https"
        http_client = urllib3.PoolManager(
            cert_reqs="CERT_REQUIRED" if secure else "CERT_NONE",
            ca_certs=ca_bundle if secure else None,
        )
        self.client = Minio(
            service.netloc,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
            http_client=http_client,
        )

    @staticmethod
    def _key(tenant_id: str, key: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", tenant_id):
            raise ValueError("invalid tenant ID for object key")
        normalized = key.strip("/")
        if not normalized or ".." in normalized.split("/"):
            raise ValueError("invalid object key")
        return f"tenants/{tenant_id}/{normalized}"

    def health(self) -> bool:
        try:
            return self.client.bucket_exists(self.bucket)
        except Exception:
            return False

    def put(self, tenant_id: str, key: str, content: bytes, *, content_type: str) -> ObjectInfo:
        if not content:
            raise ValueError("empty objects are not accepted")
        full_key = self._key(tenant_id, key)
        digest = hashlib.sha256(content).hexdigest()
        result = self.client.put_object(
            self.bucket,
            full_key,
            io.BytesIO(content),
            length=len(content),
            content_type=content_type,
            metadata={"sha256": digest, "tenant-id": tenant_id},
        )
        if not result.etag:
            raise RuntimeError("object store returned no ETag")
        return ObjectInfo(
            self.bucket,
            full_key,
            result.etag,
            result.version_id,
            digest,
            len(content),
        )

    def get(self, tenant_id: str, key: str, *, expected_sha256: str) -> bytes:
        full_key = self._key(tenant_id, key)
        response = self.client.get_object(self.bucket, full_key)
        try:
            content = response.read()
        finally:
            response.close()
            response.release_conn()
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected_sha256:
            raise RuntimeError("object digest verification failed")
        return content
