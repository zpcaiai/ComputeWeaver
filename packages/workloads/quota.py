from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from threading import RLock

from packages.persistence.postgres import PostgresRuntime


@dataclass(frozen=True, slots=True)
class Quota:
    max_gpus: int
    max_gpu_hours: Decimal
    max_concurrent_jobs: int


class QuotaLedger:
    def __init__(self, runtime: PostgresRuntime | None = None) -> None:
        self._runtime = runtime
        self._quotas: dict[str, Quota] = {}
        self._usage: dict[str, tuple[int, Decimal, int]] = {}
        self._reservations: dict[tuple[str, str], tuple[int, Decimal]] = {}
        self._lock = RLock()

    def configure(self, tenant_id: str, quota: Quota) -> None:
        if min(quota.max_gpus, quota.max_concurrent_jobs) < 0 or quota.max_gpu_hours < 0:
            raise ValueError("quota limits cannot be negative")
        if self._runtime:
            with self._runtime.tenant_connection(tenant_id) as connection:
                row = connection.execute(
                    """
                    INSERT INTO quota_ledgers(
                      tenant_id, max_gpus, max_gpu_hours, max_concurrent_jobs
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (tenant_id) DO UPDATE SET
                      max_gpus = EXCLUDED.max_gpus,
                      max_gpu_hours = EXCLUDED.max_gpu_hours,
                      max_concurrent_jobs = EXCLUDED.max_concurrent_jobs,
                      version = quota_ledgers.version + 1,
                      updated_at = now()
                    WHERE quota_ledgers.used_gpus <= EXCLUDED.max_gpus
                      AND quota_ledgers.used_gpu_hours <= EXCLUDED.max_gpu_hours
                      AND quota_ledgers.active_jobs <= EXCLUDED.max_concurrent_jobs
                    RETURNING tenant_id
                    """,
                    (tenant_id, quota.max_gpus, quota.max_gpu_hours, quota.max_concurrent_jobs),
                ).fetchone()
                if not row:
                    raise RuntimeError("quota limits cannot be reduced below current usage")
            return
        with self._lock:
            used_gpus, used_hours, jobs = self._usage.get(tenant_id, (0, Decimal(0), 0))
            if used_gpus > quota.max_gpus or used_hours > quota.max_gpu_hours or jobs > quota.max_concurrent_jobs:
                raise RuntimeError("quota limits cannot be reduced below current usage")
            self._quotas[tenant_id] = quota

    def reserve(
        self,
        tenant_id: str,
        gpus: int,
        gpu_hours: Decimal,
        *,
        reservation_key: str | None = None,
    ) -> bool:
        if gpus < 0 or gpu_hours < 0:
            raise ValueError("quota reservation cannot be negative")
        if self._runtime:
            if not reservation_key:
                raise ValueError("persistent quota reservations require a key")
            with self._runtime.tenant_connection(tenant_id) as connection:
                prior = connection.execute(
                    """
                    SELECT gpus, gpu_hours, released_at FROM quota_reservations
                    WHERE tenant_id = %s AND reservation_key = %s
                    """,
                    (tenant_id, reservation_key),
                ).fetchone()
                if prior:
                    if int(prior["gpus"]) != gpus or Decimal(prior["gpu_hours"]) != gpu_hours:
                        raise ValueError("quota reservation key reused with different demand")
                    return prior["released_at"] is None
                updated = connection.execute(
                    """
                    UPDATE quota_ledgers SET
                      used_gpus = used_gpus + %s,
                      used_gpu_hours = used_gpu_hours + %s,
                      active_jobs = active_jobs + 1,
                      version = version + 1,
                      updated_at = now()
                    WHERE tenant_id = %s
                      AND used_gpus + %s <= max_gpus
                      AND used_gpu_hours + %s <= max_gpu_hours
                      AND active_jobs + 1 <= max_concurrent_jobs
                    RETURNING tenant_id
                    """,
                    (gpus, gpu_hours, tenant_id, gpus, gpu_hours),
                ).fetchone()
                if not updated:
                    return False
                connection.execute(
                    """
                    INSERT INTO quota_reservations(tenant_id, reservation_key, gpus, gpu_hours)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (tenant_id, reservation_key, gpus, gpu_hours),
                )
                return True
        with self._lock:
            if reservation_key and (tenant_id, reservation_key) in self._reservations:
                existing = self._reservations[(tenant_id, reservation_key)]
                if existing != (gpus, gpu_hours):
                    raise ValueError("quota reservation key reused with different demand")
                return True
            quota = self._quotas.get(tenant_id)
            if quota is None:
                return False
            used_gpus, used_hours, jobs = self._usage.get(tenant_id, (0, Decimal(0), 0))
            if (
                used_gpus + gpus > quota.max_gpus
                or used_hours + gpu_hours > quota.max_gpu_hours
                or jobs + 1 > quota.max_concurrent_jobs
            ):
                return False
            self._usage[tenant_id] = (used_gpus + gpus, used_hours + gpu_hours, jobs + 1)
            if reservation_key:
                self._reservations[(tenant_id, reservation_key)] = (gpus, gpu_hours)
            return True

    def release(
        self,
        tenant_id: str,
        gpus: int,
        gpu_hours: Decimal,
        *,
        reservation_key: str | None = None,
    ) -> None:
        if self._runtime:
            if not reservation_key:
                raise ValueError("persistent quota release requires a reservation key")
            with self._runtime.tenant_connection(tenant_id) as connection:
                row = connection.execute(
                    """
                    UPDATE quota_reservations SET released_at = now()
                    WHERE tenant_id = %s AND reservation_key = %s AND released_at IS NULL
                      AND gpus = %s AND gpu_hours = %s
                    RETURNING reservation_key
                    """,
                    (tenant_id, reservation_key, gpus, gpu_hours),
                ).fetchone()
                if not row:
                    return
                connection.execute(
                    """
                    UPDATE quota_ledgers SET
                      used_gpus = greatest(0, used_gpus - %s),
                      used_gpu_hours = greatest(0, used_gpu_hours - %s),
                      active_jobs = greatest(0, active_jobs - 1),
                      version = version + 1,
                      updated_at = now()
                    WHERE tenant_id = %s
                    """,
                    (gpus, gpu_hours, tenant_id),
                )
            return
        with self._lock:
            used_gpus, used_hours, jobs = self._usage[tenant_id]
            self._usage[tenant_id] = (
                max(0, used_gpus - gpus),
                max(Decimal(0), used_hours - gpu_hours),
                max(0, jobs - 1),
            )

    def usage(self, tenant_id: str) -> tuple[int, Decimal, int]:
        if self._runtime:
            with self._runtime.tenant_connection(tenant_id) as connection:
                row = connection.execute(
                    """
                    SELECT used_gpus, used_gpu_hours, active_jobs
                    FROM quota_ledgers WHERE tenant_id = %s
                    """,
                    (tenant_id,),
                ).fetchone()
                if not row:
                    return 0, Decimal(0), 0
                return int(row["used_gpus"]), Decimal(row["used_gpu_hours"]), int(row["active_jobs"])
        return self._usage.get(tenant_id, (0, Decimal(0), 0))
