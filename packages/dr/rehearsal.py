from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg
from minio.error import S3Error
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from packages.objectstore.s3 import S3ObjectStore
from packages.secrets import CredentialResolver


@dataclass(frozen=True, slots=True)
class RehearsalCheck:
    name: str
    status: str
    started_at: datetime
    finished_at: datetime
    details: dict[str, Any]
    reason: str | None = None


def _database_name(database_url: str) -> str:
    name = urlparse(database_url.replace("postgresql+psycopg://", "postgresql://", 1)).path.lstrip("/")
    if not name:
        raise ValueError("PostgreSQL URL must include a database name")
    return name


def validate_postgres_targets(source_url: str, restore_url: str) -> None:
    source_name = _database_name(source_url)
    restore_name = _database_name(restore_url)
    if not restore_name.startswith("computeweaver_restore_"):
        raise ValueError("restore database name must start with computeweaver_restore_")
    if source_url == restore_url or source_name == restore_name:
        raise ValueError("source and restore databases must be different")


def validate_restore_contract(
    configuration: dict[str, Any],
    *,
    postgres_contract: dict[str, Any],
    object_contract: dict[str, Any],
) -> None:
    postgres = configuration.get("postgres")
    objects = configuration.get("object_store")
    if not isinstance(postgres, dict) or not isinstance(objects, dict):
        raise ValueError("restore configuration must include PostgreSQL and object-store targets")
    source_database = _database_name(str(postgres["source_url"]))
    restore_database = _database_name(str(postgres["restore_url"]))
    if source_database != postgres_contract.get("source_database"):
        raise ValueError("PostgreSQL source does not match the approved recovery target")
    if restore_database != postgres_contract.get("restore_database"):
        raise ValueError("PostgreSQL restore database does not match the approved recovery target")
    source = dict(objects["source"])
    destination = dict(objects["destination"])
    source_bucket = urlparse(str(source["bucket"])).netloc
    destination_bucket = urlparse(str(destination["bucket"])).netloc
    expected = {
        "source_bucket": source_bucket,
        "restore_bucket": destination_bucket,
        "source_prefix": str(objects["source_prefix"]).strip("/"),
        "destination_prefix": str(objects["destination_prefix"]).strip("/"),
    }
    for name, actual in expected.items():
        if object_contract.get(name) != actual:
            raise ValueError(f"object-store {name} does not match the approved recovery target")


def enforce_recovery_objectives(
    report: dict[str, Any],
    *,
    postgres_contract: dict[str, Any],
    object_contract: dict[str, Any],
) -> dict[str, Any]:
    contracts = {"postgres_restore": postgres_contract, "object_restore": object_contract}
    for raw in report.get("checks", []):
        if not isinstance(raw, dict) or raw.get("name") not in contracts:
            continue
        contract = contracts[str(raw["name"])]
        maximum = min(1800, float(contract.get("maximum_duration_seconds", 1800)))
        started = raw.get("started_at")
        finished = raw.get("finished_at")
        if not isinstance(started, datetime) or not isinstance(finished, datetime):
            raw["status"] = "FAIL"
            raw["reason"] = "recovery timing evidence is missing"
            continue
        duration = (finished - started).total_seconds()
        raw_details = raw.get("details")
        details = dict(raw_details) if isinstance(raw_details, dict) else {}
        details.update({"duration_seconds": duration, "maximum_duration_seconds": maximum})
        raw["details"] = details
        if maximum <= 0 or duration > maximum:
            raw["status"] = "FAIL"
            raw["reason"] = "recovery duration exceeded the approved objective"
    statuses = {str(check.get("status")) for check in report.get("checks", []) if isinstance(check, dict)}
    report["status"] = "FAIL" if "FAIL" in statuses else "PASS" if statuses == {"PASS"} else "NOT_RUN"
    report["recovery_objectives"] = contracts
    return report


def _pg_environment(database_url: str) -> tuple[dict[str, str], str]:
    values = conninfo_to_dict(database_url.replace("postgresql+psycopg://", "postgresql://", 1))
    mapping = {
        "host": "PGHOST",
        "hostaddr": "PGHOSTADDR",
        "port": "PGPORT",
        "user": "PGUSER",
        "password": "PGPASSWORD",
        "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT",
        "sslcert": "PGSSLCERT",
        "sslkey": "PGSSLKEY",
    }
    environment = dict(os.environ)
    for source, target in mapping.items():
        if values.get(source):
            environment[target] = str(values[source])
    return environment, str(values.get("dbname") or _database_name(database_url))


def postgres_fingerprint(database_url: str) -> dict[str, Any]:
    tables: list[dict[str, Any]] = []
    with psycopg.connect(database_url) as connection:
        rows = connection.execute(
            """
            SELECT schemaname, tablename
            FROM pg_catalog.pg_tables
            WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY schemaname, tablename
            """
        ).fetchall()
        for schema, table in rows:
            count = connection.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table))
            ).fetchone()
            tables.append({"schema": schema, "table": table, "rows": int(count[0]) if count else 0})
    digest = hashlib.sha256(json.dumps(tables, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"tables": tables, "sha256": digest}


def rehearse_postgres_restore(source_url: str, restore_url: str, *, timeout_seconds: int = 900) -> RehearsalCheck:
    started = datetime.now(UTC)
    try:
        validate_postgres_targets(source_url, restore_url)
        before_target = postgres_fingerprint(restore_url)
        if before_target["tables"]:
            raise ValueError("restore database must be empty before rehearsal")
        source_environment, source_database = _pg_environment(source_url)
        target_environment, target_database = _pg_environment(restore_url)
        pg_dump = shutil.which("pg_dump")
        pg_restore = shutil.which("pg_restore")
        if not pg_dump or not pg_restore:
            raise RuntimeError("pg_dump and pg_restore binaries are required")
        with tempfile.TemporaryDirectory(prefix="computeweaver-pg-restore-") as temporary:
            dump_path = Path(temporary) / "backup.dump"
            dump = subprocess.run(  # noqa: S603,S607 - fixed pg_dump binary with validated database name
                [
                    pg_dump,
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    f"--file={dump_path}",
                    source_database,
                ],
                env=source_environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
            if dump.returncode != 0:
                raise RuntimeError(f"pg_dump failed: {dump.stderr[-1000:]}")
            dump_sha256 = hashlib.sha256(dump_path.read_bytes()).hexdigest()
            restore = subprocess.run(  # noqa: S603,S607 - fixed pg_restore binary and isolated target
                [
                    pg_restore,
                    "--exit-on-error",
                    "--no-owner",
                    "--no-privileges",
                    f"--dbname={target_database}",
                    str(dump_path),
                ],
                env=target_environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
            if restore.returncode != 0:
                raise RuntimeError(f"pg_restore failed: {restore.stderr[-1000:]}")
        source = postgres_fingerprint(source_url)
        restored = postgres_fingerprint(restore_url)
        if source != restored:
            raise RuntimeError("restored PostgreSQL table fingerprint does not match source")
        return RehearsalCheck(
            "postgres_restore",
            "PASS",
            started,
            datetime.now(UTC),
            {"database": target_database, "dump_sha256": dump_sha256, "fingerprint": restored},
        )
    except (OSError, psycopg.Error, subprocess.SubprocessError, ValueError, RuntimeError) as error:
        return RehearsalCheck(
            "postgres_restore", "FAIL", started, datetime.now(UTC), {}, f"{type(error).__name__}: {error}"
        )


def _store(configuration: dict[str, Any], resolver: CredentialResolver) -> S3ObjectStore:
    return S3ObjectStore(
        bucket_url=str(configuration["bucket"]),
        endpoint=str(configuration["endpoint"]),
        access_key=resolver.resolve(str(configuration["access_key_ref"])),
        secret_key=resolver.resolve(str(configuration["secret_key_ref"])),
        ca_bundle=(
            str(resolver.resolve_file(str(configuration["ca_bundle_ref"])))
            if configuration.get("ca_bundle_ref")
            else None
        ),
    )


def validate_object_targets(source: S3ObjectStore, destination: S3ObjectStore, destination_prefix: str) -> None:
    if source.bucket == destination.bucket:
        raise ValueError("object restore destination bucket must differ from source")
    if not destination.bucket.startswith("computeweaver-restore-"):
        raise ValueError("object restore bucket must start with computeweaver-restore-")
    if not destination_prefix.startswith("rehearsals/") or ".." in destination_prefix.split("/"):
        raise ValueError("object restore prefix must be isolated under rehearsals/")


def rehearse_object_restore(
    configuration: dict[str, Any], *, resolver: CredentialResolver | None = None
) -> RehearsalCheck:
    started = datetime.now(UTC)
    try:
        resolver = resolver or CredentialResolver.from_env()
        source = _store(dict(configuration["source"]), resolver)
        destination = _store(dict(configuration["destination"]), resolver)
        source_prefix = str(configuration["source_prefix"]).strip("/") + "/"
        destination_prefix = str(configuration["destination_prefix"]).strip("/") + "/"
        validate_object_targets(source, destination, destination_prefix)
        if not source.client.bucket_exists(source.bucket) or not destination.client.bucket_exists(destination.bucket):
            raise RuntimeError("source or restore bucket is unavailable")
        versioning = destination.client.get_bucket_versioning(destination.bucket)
        if str(versioning.status).lower() != "enabled":
            raise RuntimeError("restore bucket versioning must be enabled")
        maximum = int(configuration.get("max_objects", 1000))
        if maximum < 1 or maximum > 100_000:
            raise ValueError("object rehearsal max_objects is outside safe bounds")
        objects = list(source.client.list_objects(source.bucket, prefix=source_prefix, recursive=True))
        if not objects:
            raise RuntimeError("source object prefix is empty")
        if len(objects) > maximum:
            raise RuntimeError("source object count exceeds configured rehearsal bound")
        evidence: list[dict[str, Any]] = []
        for item in objects:
            target_key = f"{destination_prefix}{item.object_name}"
            try:
                destination.client.stat_object(destination.bucket, target_key)
                raise RuntimeError(f"restore target already exists: {target_key}")
            except S3Error as error:
                if error.code not in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                    raise
            response = source.client.get_object(source.bucket, item.object_name, version_id=item.version_id)
            try:
                content = response.read()
            finally:
                response.close()
                response.release_conn()
            digest = hashlib.sha256(content).hexdigest()
            stored = destination.client.put_object(
                destination.bucket,
                target_key,
                io.BytesIO(content),
                len(content),
                metadata={"sha256": digest, "source-version-id": item.version_id or "unversioned"},
            )
            restored_response = destination.client.get_object(
                destination.bucket, target_key, version_id=stored.version_id
            )
            try:
                restored = restored_response.read()
            finally:
                restored_response.close()
                restored_response.release_conn()
            if hashlib.sha256(restored).hexdigest() != digest:
                raise RuntimeError(f"restored object digest mismatch: {target_key}")
            evidence.append(
                {
                    "source_key": item.object_name,
                    "source_version_id": item.version_id,
                    "restore_key": target_key,
                    "restore_version_id": stored.version_id,
                    "size": len(content),
                    "sha256": digest,
                }
            )
        return RehearsalCheck(
            "object_restore",
            "PASS",
            started,
            datetime.now(UTC),
            {"source_bucket": source.bucket, "restore_bucket": destination.bucket, "objects": evidence},
        )
    except (KeyError, OSError, PermissionError, RuntimeError, S3Error, ValueError) as error:
        return RehearsalCheck(
            "object_restore", "FAIL", started, datetime.now(UTC), {}, f"{type(error).__name__}: {error}"
        )


def run_restore_rehearsal(configuration: dict[str, Any]) -> dict[str, Any]:
    checks: list[RehearsalCheck] = []
    postgres = configuration.get("postgres")
    if isinstance(postgres, dict):
        checks.append(rehearse_postgres_restore(str(postgres["source_url"]), str(postgres["restore_url"])))
    else:
        now = datetime.now(UTC)
        checks.append(RehearsalCheck("postgres_restore", "NOT_RUN", now, now, {}, "configuration missing"))
    objects = configuration.get("object_store")
    if isinstance(objects, dict):
        checks.append(rehearse_object_restore(objects))
    else:
        now = datetime.now(UTC)
        checks.append(RehearsalCheck("object_restore", "NOT_RUN", now, now, {}, "configuration missing"))
    statuses = {check.status for check in checks}
    overall = "FAIL" if "FAIL" in statuses else "PASS" if statuses == {"PASS"} else "NOT_RUN"
    return {
        "status": overall,
        "release_id": configuration.get("release_id"),
        "source_revision": configuration.get("source_revision"),
        "request_sha256": configuration.get("request_sha256"),
        "checks": [asdict(check) for check in checks],
    }
