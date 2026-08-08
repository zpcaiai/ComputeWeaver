from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class PostgresRuntime:
    """Owns the bounded PostgreSQL pool, tenant transaction context, and migrations."""

    def __init__(
        self,
        database_url: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        connect_timeout_seconds: int = 5,
    ) -> None:
        if database_url.startswith("postgresql+psycopg://"):
            database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        self._pool = ConnectionPool(
            conninfo=database_url,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={
                "autocommit": False,
                "connect_timeout": connect_timeout_seconds,
                "row_factory": dict_row,
                "application_name": "computeweaver",
            },
            check=ConnectionPool.check_connection,
        )

    def open(self, *, wait: bool = True) -> None:
        if self._pool.closed:
            self._pool.open(wait=wait)

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def connection(self) -> Iterator[Connection[Any]]:
        self.open()
        with self._pool.connection() as connection:
            yield connection

    @contextmanager
    def tenant_connection(self, tenant_id: str, actor_id: str = "system") -> Iterator[Connection[Any]]:
        if not tenant_id or "\x00" in tenant_id:
            raise ValueError("invalid tenant ID")
        with self.connection() as connection:
            connection.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            connection.execute("SELECT set_config('app.actor_id', %s, true)", (actor_id,))
            yield connection

    def health(self) -> bool:
        try:
            with self.connection() as connection:
                row = connection.execute(
                    """
                    SELECT
                      to_regclass('public.resources') IS NOT NULL AS resources_ready,
                      to_regclass('public.schema_migrations') IS NOT NULL AS migrations_ready
                    """
                ).fetchone()
                return bool(row and row["resources_ready"] and row["migrations_ready"])
        except Exception:
            return False

    def migrate(self) -> tuple[int, ...]:
        """Apply checksum-locked migrations while holding a cluster-wide advisory lock."""
        package = resources.files("packages.persistence.migrations")
        candidates = sorted(
            (item for item in package.iterdir() if item.name.endswith(".sql")),
            key=lambda item: item.name,
        )
        applied: list[int] = []
        with self.connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(493774221)")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                  version bigint PRIMARY KEY,
                  applied_at timestamptz NOT NULL DEFAULT now(),
                  checksum text NOT NULL
                )
                """
            )
            existing_rows = connection.execute(
                "SELECT version, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
            existing = {
                int(row["version"]): (
                    row["checksum"].decode() if isinstance(row["checksum"], bytes) else str(row["checksum"])
                )
                for row in existing_rows
            }
            for candidate in candidates:
                prefix = candidate.name.split("_", 1)[0]
                if not prefix.isdigit():
                    raise RuntimeError(f"migration filename must start with a number: {candidate.name}")
                version = int(prefix)
                sql = candidate.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                if version in existing:
                    if existing[version] != checksum:
                        raise RuntimeError(f"applied migration checksum changed: {candidate.name}")
                    continue
                connection.execute(sql)
                connection.execute(
                    "INSERT INTO schema_migrations(version, checksum) VALUES (%s, %s)",
                    (version, checksum),
                )
                applied.append(version)
        return tuple(applied)
