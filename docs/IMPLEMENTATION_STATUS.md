# Skill implementation status

The source skill package is treated as an immutable specification. Runtime code, tests and
generated evidence live in the repository outside `skills/`.

| Batch | Code-level capability | Primary implementation | Local gate |
|---|---|---|---|
| B01 | Reproducible platform foundation | `apps/`, `deploy/`, `requirements*.lock`, CI | Clean Git commit/tree binding, registry fallback, verified offline image bundles, disk preflight and non-root/read-only/cap-drop smoke implemented; current image revalidation remains environment-dependent |
| B02 | Domain types and API contracts | `packages/domain`, `packages/contracts`, `schemas/` | Implemented and locally verified |
| B03 | Versioned physical topology | `packages/topology`, PostgreSQL topology tables | Durable path integration-tested |
| B04 | Compute inventory and read-only adapters | `packages/compute`, Kubernetes/Slurm/Prometheus connectors | Live protocols and 10,000-node serialization gate implemented; real credentials `NOT_RUN` |
| B05 | Workloads, lifecycle, quota and admission | `packages/workloads`, `packages/admission`, durable quota ledger | Durable path integration-tested |
| B06 | Tariff, calendar, cost and carbon | `packages/tariffs`, `packages/region_packs`, `packages/carbon` | Implemented and locally verified |
| B07 | Energy assets and power constraints | `packages/energy` | Implemented and locally verified |
| B08 | Ingestion, lineage, quality and time series | `packages/ingestion`, `packages/data_quality`, `packages/timeseries`, meter connector | Durable path integration-tested; live meter `NOT_RUN` |
| B09 | Deterministic digital twin | `packages/simulation`, `apps/simulator` | Implemented and locally verified |
| B10 | Scenario compiler, faults, replay and evaluation | `packages/scenarios`, `packages/faults`, `packages/replay`, `packages/evaluator` | Implemented and locally verified |
| B11 | Forecasting, backtest, registry and fallback | `packages/forecasting` | Implemented and locally verified |
| B12 | Deterministic scheduling baselines and benchmark | `packages/scheduling`, `packages/benchmark` | Implemented and locally verified |
| B13 | Exact and HiGHS MILP optimization | `packages/optimization` | Implemented and locally verified |
| B14 | Receding-horizon MPC and state reconciliation | `packages/mpc` | Implemented and locally verified |
| B15 | Policy, constraints, governed plan lifecycle | `packages/policy`, `packages/constraints`, `packages/plans`, `packages/risk` | Persistent policy path implemented |
| B16 | Approval, Action Guard, idempotency and compensation | `packages/approval`, `packages/execution` | Durable approval/action path implemented; provider execution stays gated |
| B17 | Explanation, counterfactuals and reconciled reports | `packages/explain`, `packages/whatif`, `packages/reports` | Implemented and locally verified |
| B18 | IAM, budgets, chargeback, notifications and admin config | `packages/iam`, PostgreSQL RLS, OIDC/JWKS, durable resource history, admin rollback, operator console | Signed-auth, OIDC PKCE UI and RLS code implemented; live IdP `NOT_RUN` |
| B19 | Multi-site, sovereignty, islanding and DR | `packages/multisite`, `packages/sovereignty`, `packages/island`, `packages/resilience`, `packages/dr` | Isolated PostgreSQL and versioned-object restore rehearsal code implemented; real recovery targets still required |
| B20 | Fail-closed release certification | `packages/certification`, `scripts/generate_evidence.py`, `deploy/kubernetes/production-gates.yaml` | Immutable evidence request, production preflight, aggregate real-gate runner, portable JWT/artifact re-verification, signed release-token lifecycle, hash-chained events, revocation registry, hardened Gate Jobs and operator readiness view implemented; `NOT_CERTIFIED` until real bound evidence passes |

The production code path now includes PostgreSQL-backed state, checksum-locked migrations,
RLS, a leased retry/dead-letter worker, durable approvals/quota/action idempotency, OIDC/JWKS,
OpenTelemetry OTLP, S3-compatible versioned artifacts, live read-only Kubernetes/Slurm/
Prometheus/meter adapters, signed-release-gated provider execution, tenant-scoped immutable
resource history, durable admin rollback, an OIDC PKCE operator console behind a constrained
same-origin reverse proxy, release-bound load/connector/restore gates, and independent
penetration plus three-owner human-acceptance signature verification. External gate reports
share one expiring evidence-request digest, bind actual artifact hashes, and are rejected if a
SHA-256 sidecar, release, source revision or cryptographic signature is missing or changed. The
signer trust policy is separately request-bound, attestation expiry is rechecked at certification,
and the release certificate hashes every declared gate artifact. A certificate can only be
published after a matching persisted run and public-key verification of its release token;
revoked releases cannot be republished. Prometheus alert rules, backup/restore, incident and
rollback runbooks, and a suspended hardened PostgreSQL backup CronJob template are included.

This does not turn unavailable external evidence into a pass. An unversioned or dirty checkout
keeps B01-B19 at `EVIDENCE_PENDING`, while a clean local or CI Git commit can bind those artifacts
to the exact commit and tree. B20 remains `NOT_CERTIFIED` until the implemented runners are executed against real IdP,
Kubernetes, Slurm, meter, EMS, production load, PostgreSQL and object-storage recovery targets,
and independent assessor/owner signatures. No code path converts a local simulation, a placeholder
configuration or a self-issued assertion into production evidence.
