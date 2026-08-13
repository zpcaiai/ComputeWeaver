# ComputeWeaver

ComputeWeaver is a safety-first AI compute and energy control plane. It implements the
twenty batches in `skills/ai-compute-energy-control-plane-skills` as executable Python
services, deterministic algorithms, a FastAPI surface, a Vue operator shell, tests and
evidence generation.

The production profile is fail-closed: PostgreSQL, S3-compatible object storage, signed OIDC
identity, and valid configuration must all be healthy before readiness succeeds. External
scheduler or EMS mutation additionally requires guarded mode, a fixed mTLS-capable provider
gateway, and a signed B20 release token bound to the running commit.

```bash
make bootstrap
make verify
make evidence
make dev
```

Production evidence uses explicit, fail-closed commands rather than simulator substitution:

```bash
PRODUCTION_PREFLIGHT_CONFIG=config/production-preflight.json make production-preflight
make container-verify
# On a connected build host (repeat --reference for the four pinned Compose/base images):
.venv/bin/python -m scripts.export_image_bundle --destination /secure-transfer/bundle --reference IMAGE@sha256:DIGEST
# On the isolated host:
.venv/bin/python -m scripts.manage_image_bundle /secure-transfer/image-bundle.json --load
# The same bundle can eliminate base-image registry pulls during the hardened build:
IMAGE_BUNDLE=/secure-transfer/image-bundle.json make container-verify
EVIDENCE_REQUEST_CONFIG=config/production-evidence-request.json make evidence-request
ACCEPTANCE_MANIFEST=config/production-acceptance.json make external-acceptance
LOAD_TARGET=https://control.example.com/v1/compute/nodes LOAD_TOKEN_REF=secret://LOAD_TOKEN RELEASE_ID=... SOURCE_REVISION=... make production-load
RESTORE_CONFIG=config/restore-rehearsal.json make restore-rehearsal
ATTESTATION_SIGN_CONFIG=config/owner-signing.json ATTESTATION_OUTPUT=evidence/B20/attestations/owner.jwt make issue-attestation
ATTESTATION_BUNDLE=config/production-attestations.json ATTESTATION_POLICY=config/production-attestation-trust-policy.json make verify-attestations
EXTERNAL_GATE_SUITE_CONFIG=config/external-gate-suite.json make external-gate-suite
certify external-status --release-id "$RELEASE_ID" --commit "$SOURCE_REVISION"
certify run --commit "$SOURCE_REVISION" --release-id "$RELEASE_ID" --actor "$RELEASE_OPERATOR"
certify report --release-id "$RELEASE_ID"
certify release --commit "$SOURCE_REVISION" --release-id "$RELEASE_ID" --actor "$RELEASE_OPERATOR" --signing-key /var/run/computeweaver-signing/release-private.pem --verification-key /var/run/computeweaver-signing/release-public.pem --key-id "$RELEASE_KEY_ID"
```

The evidence request binds an immutable source revision and input artifact hashes to every
external gate. Each generated report has a SHA-256 sidecar and JUnit result. Penetration testing
is signed by an approved independent assessor; product, security and operations acceptance
requires three distinct signing subjects. The verifier trust policy is a separately managed
input artifact whose hash is fixed by that evidence request, so a submitted bundle cannot choose
its own issuer, signer allowlist or public key. Private signing keys must be owner-only (`0600`) and
are never written into evidence. The example JSON files contain references only; secrets are
resolved from controlled `secret://` or `file://` providers. Missing, expired, tampered,
mismatched or unsigned production evidence remains `NOT_RUN`/`NOT_CERTIFIED`.

The immutable-source check requires the release directory itself to be the Git worktree root,
verifies both the commit and tree objects, and rejects tracked or untracked changes. Production
JUnit and coverage artifacts receive their own clean-commit/tree binding after each test run, so
results from an earlier revision cannot satisfy the current release gate. Production
preflight additionally validates disk reserve, required executables, the Docker daemon, digest
pins, non-local HTTPS endpoints, placeholder-free JSON configuration and resolvable secret
references without printing secret values. A verified offline image bundle provides a bounded
alternative to an unstable registry: archive hashes are checked before loading, available disk is
checked before expansion, and every loaded RepoDigest is checked again. The aggregate external
gate suite runs preflight, IdP/Kubernetes/Slurm/meter/EMS acceptance, production load, isolated
database/object restore, penetration attestation and the three distinct owner signatures, then
revalidates every output against the same evidence-request digest.
`certify external-status` emits an integrity-protected, machine-readable view of the five external
gates with their exact evidence references and next commands. The same view is available from
`GET /v1/certification/{release_id}/external-readiness`; neither interface can promote a failed gate.

The operator console exposes all 20 skill workspaces from a generated OpenAPI catalog. It includes
every REST operation exactly once, typed path/query/body controls, idempotency and optimistic
concurrency headers, correlation IDs, structured results, audit/compensation guidance and explicit
confirmation for high-risk mutations. B20 adds governed evidence-request, run, publish, event-chain
inspection and revocation controls; publication re-evaluates the immutable evidence and signs only a
matching `CERTIFIED` run. The server-side signing key is read from a read-only secret mount and is
never returned to the browser.

The final evaluator re-verifies the independent JWTs, policy, bound public keys and signed
artifact hashes instead of trusting a report flag. Published certificates are stored by release,
include test/scenario/approval metadata, emit append-only lifecycle events and can be revoked with
`certify revoke --release-id ... --actor ... --reason ...`. Guarded execution requires the current
verified revocation registry and rejects revoked certificate hashes.

`deploy/kubernetes/production-gates.yaml` provides non-root, read-only, suspended Jobs for the
external integration, production load, restore and signature-verification gates. Release
operators must supply the immutable image digest, encrypted evidence volume, ConfigMaps and
Secrets, then unsuspend each Job explicitly.
The API deployment shares the `computeweaver-release-evidence` RWX claim with those Jobs and expects
the `computeweaver-release-signing` Secret to contain `release-private.pem` and
`release-public.pem`. Replace the immutable commit and controlled key ID placeholders before rollout.

The local simulator API is then available at `http://127.0.0.1:8000`, including `/docs`, `/health/live`,
`/health/ready` and `/version`. The durable Compose stack also exposes the same-origin operator console at
`http://127.0.0.1:8080`; it reverse-proxies only the allowlisted API surface and uses a simulator identity only
outside staging/production.

For the durable local stack, run `docker compose -f deploy/compose/docker-compose.yml up --build`.
The finite simulator workload is excluded from the default long-running stack; run it explicitly with
`docker compose -f deploy/compose/docker-compose.yml --profile simulation up --build simulator`.
The stack provisions separate migrator, application, and RLS-bypass worker database roles,
runs checksum-locked migrations, enables object versioning, then starts the non-root API and
leased worker with a read-only root filesystem, all capabilities dropped and
`no-new-privileges`. Production deployments must inject the secret references and replace every
`REPLACE_*` value in `deploy/kubernetes/base.yaml`. The web console uses OIDC Authorization Code + PKCE,
stores access tokens only in session storage, and requires `COMPUTEWEAVER_WEB_OIDC_CLIENT_ID` in production.
The Compose project has the fixed name `computeweaver`. `make docker-inspect` lists only resources
carrying that project label. If disk must be recovered, `PROJECT_DOCKER_APPLY=1 make docker-clean`
removes only this project's local images, containers, networks and volumes; it never invokes a
global Docker prune or touches another Compose project.

`make verify` runs lint, static typing, generated-contract drift checks, Python tests with
coverage, the Vue production build, Vitest catalog tests, a real Chromium workflow/accessibility
smoke with an immutable source binding, skill-package validation and a deliberate CI-failure check. CI also starts PostgreSQL and
executes the durable production-path integration test.
`make evidence` writes batch-scoped results under `evidence/B01` through `evidence/B20`.
See `docs/IMPLEMENTATION_STATUS.md` for the batch-to-code map and certification boundaries.
