from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import RLock

from packages.persistence.postgres import PostgresRuntime


@dataclass(frozen=True, slots=True)
class ConnectorOffset:
    cursor: str | None
    watermark: datetime | None


class ConnectorOffsetStore:
    def __init__(self, runtime: PostgresRuntime | None = None) -> None:
        self.runtime = runtime
        self._memory: dict[tuple[str, str, str], ConnectorOffset] = {}
        self._lock = RLock()

    def get(self, tenant_id: str, connector_id: str, stream: str) -> ConnectorOffset:
        if self.runtime:
            with self.runtime.tenant_connection(tenant_id) as connection:
                row = connection.execute(
                    """
                    SELECT cursor_value, watermark
                    FROM connector_offsets
                    WHERE tenant_id = %s AND connector_id = %s AND stream = %s
                    """,
                    (tenant_id, connector_id, stream),
                ).fetchone()
                return ConnectorOffset(
                    str(row["cursor_value"]) if row and row["cursor_value"] is not None else None,
                    row["watermark"] if row else None,
                )
        with self._lock:
            return self._memory.get((tenant_id, connector_id, stream), ConnectorOffset(None, None))

    def commit(
        self,
        tenant_id: str,
        connector_id: str,
        stream: str,
        *,
        cursor: str | None,
        watermark: datetime,
    ) -> ConnectorOffset:
        offset = ConnectorOffset(cursor, watermark)
        if self.runtime:
            with self.runtime.tenant_connection(tenant_id) as connection:
                connection.execute(
                    """
                    INSERT INTO connector_offsets(tenant_id, connector_id, stream, cursor_value, watermark)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, connector_id, stream) DO UPDATE SET
                      cursor_value = EXCLUDED.cursor_value,
                      watermark = EXCLUDED.watermark,
                      updated_at = now()
                    """,
                    (tenant_id, connector_id, stream, cursor, watermark),
                )
            return offset
        with self._lock:
            self._memory[(tenant_id, connector_id, stream)] = offset
            return offset
