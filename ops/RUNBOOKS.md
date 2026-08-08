# Runbooks

## Connector degradation

Open the circuit, retain last-known state with degraded quality, block automated actions when
quality crosses policy threshold, and verify recovery through a dry-run before closing.

## Solver timeout

Retain the best feasible solution if independently validated; otherwise use the last-safe MPC
plan. Never relax a hard constraint to obtain feasibility.

## External state drift

Stop execution, invalidate affected approvals, compare observed state with the approved plan,
and require a fresh plan plus approval before resuming.

## Restore

Restore database and object evidence, verify hashes, then reconcile all planned actions with
external state before enabling guarded mode.

1. Force `COMPUTEWEAVER_EXTERNAL_WRITE_ENABLED=false` and scale the worker to zero.
2. Restore PostgreSQL into an isolated database and run `python -m packages.persistence.cli check`.
3. Verify the per-tenant audit hash chain and every restored object SHA-256 before cutover.
4. Start the API in read-only mode, reconcile scheduler/EMS state, then execute fault scenarios.
5. Re-enable workers only after a new B20 certificate is signed for the restored commit.

## Durable worker recovery

Expired leases are reclaimed with `FOR UPDATE SKIP LOCKED`. Repeated failures use exponential
backoff and end in `dead_letter`; operators must inspect the recorded error and external
idempotency state before requeueing. A worker database role is intentionally separate and is
the only runtime role granted `BYPASSRLS` for cross-tenant claiming.

## Identity key rotation

Publish the new asymmetric signing key at the configured JWKS URL before issuing tokens. Keep
the prior key through the maximum token lifetime and JWKS cache TTL, then remove it. Never
enable trusted-header authentication outside the simulator/test profiles.
