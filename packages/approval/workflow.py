from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from threading import RLock

from packages.persistence.postgres import PostgresRuntime
from packages.risk.classifier import RiskLevel


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    id: str
    plan_id: str
    tenant_id: str
    risk: RiskLevel
    requested_by: str
    expires_at: datetime
    required_roles: frozenset[str]
    required_count: int
    approvals: tuple[tuple[str, str], ...] = ()
    status: ApprovalStatus = ApprovalStatus.PENDING


class ApprovalWorkflow:
    def __init__(self, runtime: PostgresRuntime | None = None) -> None:
        self._runtime = runtime
        self._requests: dict[str, ApprovalRequest] = {}
        self._lock = RLock()

    def create(self, request: ApprovalRequest) -> None:
        minimum = 2 if request.risk >= RiskLevel.L3 else (1 if request.risk >= RiskLevel.L2 else 0)
        if request.required_count < minimum:
            raise ValueError("approval count below risk minimum")
        if request.required_count > 0 and not request.required_roles:
            raise ValueError("approval roles are required")
        if self._runtime:
            with self._runtime.tenant_connection(request.tenant_id, request.requested_by) as connection:
                existing = connection.execute(
                    "SELECT * FROM approvals WHERE tenant_id = %s AND id = %s",
                    (request.tenant_id, request.id),
                ).fetchone()
                if existing:
                    same_request = (
                        str(existing["plan_id"]) == request.plan_id
                        and int(existing["risk"]) == int(request.risk)
                        and str(existing["requested_by"]) == request.requested_by
                        and existing["expires_at"] == request.expires_at
                        and frozenset(str(item) for item in existing["required_roles"]) == request.required_roles
                        and int(existing["required_count"]) == request.required_count
                    )
                    if same_request:
                        return
                    raise ValueError("approval ID already exists with different requirements")
                connection.execute(
                    """
                    INSERT INTO approvals(
                      id, tenant_id, plan_id, risk, requested_by, expires_at,
                      required_roles, required_count, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        request.id,
                        request.tenant_id,
                        request.plan_id,
                        int(request.risk),
                        request.requested_by,
                        request.expires_at,
                        list(request.required_roles),
                        request.required_count,
                        request.status.value,
                    ),
                )
            return
        with self._lock:
            if request.id in self._requests:
                if self._requests[request.id] == request:
                    return
                raise ValueError("approval ID already exists with different requirements")
            self._requests[request.id] = request

    def approve(
        self,
        request_id: str,
        *,
        actor_id: str,
        role: str,
        now: datetime,
        tenant_id: str | None = None,
    ) -> ApprovalRequest:
        if self._runtime:
            if not tenant_id:
                raise ValueError("tenant_id is required for persistent approvals")
            with self._runtime.tenant_connection(tenant_id, actor_id) as connection:
                row = connection.execute(
                    "SELECT * FROM approvals WHERE tenant_id = %s AND id = %s FOR UPDATE",
                    (tenant_id, request_id),
                ).fetchone()
                if not row:
                    raise KeyError(request_id)
                if row["status"] != ApprovalStatus.PENDING.value:
                    raise ValueError("approval is not pending")
                if now >= row["expires_at"]:
                    connection.execute(
                        """
                        UPDATE approvals
                        SET status = 'expired', version = version + 1, updated_at = now()
                        WHERE tenant_id = %s AND id = %s
                        """,
                        (tenant_id, request_id),
                    )
                    raise ValueError("approval expired")
                if actor_id == row["requested_by"]:
                    raise PermissionError("requester cannot approve own high-risk action")
                if role not in row["required_roles"]:
                    raise PermissionError("actor role cannot approve request")
                duplicate = connection.execute(
                    """
                    SELECT 1 FROM approval_votes
                    WHERE tenant_id = %s AND approval_id = %s AND actor_id = %s
                    """,
                    (tenant_id, request_id, actor_id),
                ).fetchone()
                if duplicate:
                    existing_votes = connection.execute(
                        """
                        SELECT actor_id, role FROM approval_votes
                        WHERE tenant_id = %s AND approval_id = %s AND decision = 'approved'
                        ORDER BY decided_at, actor_id
                        """,
                        (tenant_id, request_id),
                    ).fetchall()
                    return ApprovalRequest(
                        id=str(row["id"]),
                        plan_id=str(row["plan_id"]),
                        tenant_id=str(row["tenant_id"]),
                        risk=RiskLevel(int(row["risk"])),
                        requested_by=str(row["requested_by"]),
                        expires_at=row["expires_at"],
                        required_roles=frozenset(str(item) for item in row["required_roles"]),
                        required_count=int(row["required_count"]),
                        approvals=tuple((str(vote["actor_id"]), str(vote["role"])) for vote in existing_votes),
                        status=ApprovalStatus(str(row["status"])),
                    )
                connection.execute(
                    """
                    INSERT INTO approval_votes(approval_id, tenant_id, actor_id, role, decision, decided_at)
                    VALUES (%s, %s, %s, %s, 'approved', %s)
                    """,
                    (request_id, tenant_id, actor_id, role, now),
                )
                count_row = connection.execute(
                    """
                    SELECT count(*) AS votes FROM approval_votes
                    WHERE tenant_id = %s AND approval_id = %s AND decision = 'approved'
                    """,
                    (tenant_id, request_id),
                ).fetchone()
                status = (
                    ApprovalStatus.APPROVED
                    if count_row and int(count_row["votes"]) >= int(row["required_count"])
                    else ApprovalStatus.PENDING
                )
                connection.execute(
                    """
                    UPDATE approvals SET status = %s, version = version + 1, updated_at = now()
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (status.value, tenant_id, request_id),
                )
            return self.get(request_id, tenant_id=tenant_id)
        with self._lock:
            request = self._requests[request_id]
            if request.status != ApprovalStatus.PENDING:
                raise ValueError("approval is not pending")
            if now >= request.expires_at:
                expired = replace(request, status=ApprovalStatus.EXPIRED)
                self._requests[request_id] = expired
                raise ValueError("approval expired")
            if actor_id == request.requested_by:
                raise PermissionError("requester cannot approve own high-risk action")
            if role not in request.required_roles:
                raise PermissionError("actor role cannot approve request")
            if any(existing == actor_id for existing, _ in request.approvals):
                return request
            approvals = request.approvals + ((actor_id, role),)
            status = ApprovalStatus.APPROVED if len(approvals) >= request.required_count else ApprovalStatus.PENDING
            updated = replace(request, approvals=approvals, status=status)
            self._requests[request_id] = updated
            return updated

    def reject(self, request_id: str, *, tenant_id: str | None = None) -> ApprovalRequest:
        if self._runtime:
            if not tenant_id:
                raise ValueError("tenant_id is required for persistent approvals")
            with self._runtime.tenant_connection(tenant_id) as connection:
                row = connection.execute(
                    """
                    UPDATE approvals SET status = 'rejected', version = version + 1, updated_at = now()
                    WHERE tenant_id = %s AND id = %s AND status = 'pending'
                    RETURNING id
                    """,
                    (tenant_id, request_id),
                ).fetchone()
                if not row:
                    existing = connection.execute(
                        "SELECT status FROM approvals WHERE tenant_id = %s AND id = %s",
                        (tenant_id, request_id),
                    ).fetchone()
                    if not existing:
                        raise KeyError(request_id)
                    if existing["status"] != ApprovalStatus.REJECTED.value:
                        raise ValueError("approval is not pending")
            return self.get(request_id, tenant_id=tenant_id)
        with self._lock:
            request = self._requests[request_id]
            if request.status == ApprovalStatus.REJECTED:
                return request
            if request.status != ApprovalStatus.PENDING:
                raise ValueError("approval is not pending")
            updated = replace(request, status=ApprovalStatus.REJECTED)
            self._requests[request_id] = updated
            return updated

    def modify(
        self,
        request_id: str,
        *,
        actor_id: str,
        expires_at: datetime,
        required_roles: frozenset[str],
        required_count: int,
        tenant_id: str | None = None,
    ) -> ApprovalRequest:
        if self._runtime:
            if not tenant_id:
                raise ValueError("tenant_id is required for persistent approvals")
            with self._runtime.tenant_connection(tenant_id, actor_id) as connection:
                row = connection.execute(
                    "SELECT * FROM approvals WHERE tenant_id = %s AND id = %s FOR UPDATE",
                    (tenant_id, request_id),
                ).fetchone()
                if not row:
                    raise KeyError(request_id)
                votes = connection.execute(
                    "SELECT count(*) AS count FROM approval_votes WHERE tenant_id = %s AND approval_id = %s",
                    (tenant_id, request_id),
                ).fetchone()
                self._validate_modification(
                    status=ApprovalStatus(str(row["status"])),
                    requested_by=str(row["requested_by"]),
                    actor_id=actor_id,
                    risk=RiskLevel(int(row["risk"])),
                    expires_at=expires_at,
                    required_roles=required_roles,
                    required_count=required_count,
                    vote_count=int(votes["count"]) if votes else 0,
                )
                connection.execute(
                    """
                    UPDATE approvals SET expires_at = %s, required_roles = %s, required_count = %s,
                      version = version + 1, updated_at = now()
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (expires_at, list(required_roles), required_count, tenant_id, request_id),
                )
            return self.get(request_id, tenant_id=tenant_id)
        with self._lock:
            request = self._requests[request_id]
            self._validate_modification(
                status=request.status,
                requested_by=request.requested_by,
                actor_id=actor_id,
                risk=request.risk,
                expires_at=expires_at,
                required_roles=required_roles,
                required_count=required_count,
                vote_count=len(request.approvals),
            )
            updated = replace(
                request,
                expires_at=expires_at,
                required_roles=required_roles,
                required_count=required_count,
            )
            self._requests[request_id] = updated
            return updated

    @staticmethod
    def _validate_modification(
        *,
        status: ApprovalStatus,
        requested_by: str,
        actor_id: str,
        risk: RiskLevel,
        expires_at: datetime,
        required_roles: frozenset[str],
        required_count: int,
        vote_count: int,
    ) -> None:
        if status != ApprovalStatus.PENDING or vote_count:
            raise ValueError("only an unvoted pending approval can be modified")
        if actor_id != requested_by:
            raise PermissionError("only the approval requester can modify it")
        minimum = 2 if risk >= RiskLevel.L3 else (1 if risk >= RiskLevel.L2 else 0)
        if required_count < minimum or (required_count and not required_roles):
            raise ValueError("approval requirements are below the risk minimum")
        if expires_at.tzinfo is None:
            raise ValueError("approval expiry must be timezone-aware")

    def get(self, request_id: str, *, tenant_id: str | None = None) -> ApprovalRequest:
        if self._runtime:
            if not tenant_id:
                raise ValueError("tenant_id is required for persistent approvals")
            with self._runtime.tenant_connection(tenant_id) as connection:
                row = connection.execute(
                    "SELECT * FROM approvals WHERE tenant_id = %s AND id = %s",
                    (tenant_id, request_id),
                ).fetchone()
                if not row:
                    raise KeyError(request_id)
                votes = connection.execute(
                    """
                    SELECT actor_id, role FROM approval_votes
                    WHERE tenant_id = %s AND approval_id = %s AND decision = 'approved'
                    ORDER BY decided_at, actor_id
                    """,
                    (tenant_id, request_id),
                ).fetchall()
                return ApprovalRequest(
                    id=str(row["id"]),
                    plan_id=str(row["plan_id"]),
                    tenant_id=str(row["tenant_id"]),
                    risk=RiskLevel(int(row["risk"])),
                    requested_by=str(row["requested_by"]),
                    expires_at=row["expires_at"],
                    required_roles=frozenset(str(item) for item in row["required_roles"]),
                    required_count=int(row["required_count"]),
                    approvals=tuple((str(vote["actor_id"]), str(vote["role"])) for vote in votes),
                    status=ApprovalStatus(str(row["status"])),
                )
        return self._requests[request_id]

    def list(self, tenant_id: str) -> tuple[ApprovalRequest, ...]:
        if self._runtime:
            with self._runtime.tenant_connection(tenant_id) as connection:
                rows = connection.execute(
                    "SELECT id FROM approvals WHERE tenant_id = %s ORDER BY id",
                    (tenant_id,),
                ).fetchall()
            return tuple(self.get(str(row["id"]), tenant_id=tenant_id) for row in rows)
        return tuple(
            sorted(
                (item for item in self._requests.values() if item.tenant_id == tenant_id),
                key=lambda item: item.id,
            )
        )
