from __future__ import annotations

import time

from minio.commonconfig import ENABLED
from minio.versioningconfig import VersioningConfig

from config.settings import Settings

from .s3 import S3ObjectStore


def main() -> None:
    settings = Settings.from_env()
    if not all(
        (
            settings.object_store_endpoint,
            settings.object_store_access_key,
            settings.object_store_secret_key,
        )
    ):
        raise SystemExit("object store endpoint and credentials are required")
    endpoint = settings.object_store_endpoint
    access_key = settings.object_store_access_key
    secret_key = settings.object_store_secret_key
    if endpoint is None or access_key is None or secret_key is None:
        raise SystemExit("object store settings failed validation")
    store = S3ObjectStore(
        bucket_url=settings.object_store,
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        ca_bundle=settings.object_store_ca_bundle,
    )
    last_error: Exception | None = None
    for _ in range(30):
        try:
            if not store.client.bucket_exists(store.bucket):
                store.client.make_bucket(store.bucket)
            store.client.set_bucket_versioning(store.bucket, VersioningConfig(ENABLED))
            if store.health():
                print(f"object store ready: {store.bucket}")
                return
        except Exception as error:
            last_error = error
            time.sleep(1)
    raise RuntimeError("object store initialization timed out") from last_error


if __name__ == "__main__":
    main()
