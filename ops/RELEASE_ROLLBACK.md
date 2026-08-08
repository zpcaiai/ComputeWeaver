# Release, rollback and revocation

1. Release from an immutable Git commit and digest-pinned image. Generate a release-bound evidence
   request that includes the trust policy and all independent signer public keys.
2. Run `certify preflight`, then `certify external-status`, then `certify run`. Resolve every failed gate; risk acceptance cannot
   replace a mandatory gate.
3. Review `certify report`. Use `certify release --signing-key ...` only with an HSM/KMS-mounted
   short-lived key and role-separated release operator.
4. Mount the signed token, public key and verified revocation registry read-only into execution
   workloads. Enable guarded mode only after the running commit matches the token.
5. Roll back by disabling external writes, reconciling external state, deploying the prior digest
   and issuing a new certificate for that exact deployment. Never reuse a certificate across commits.
6. Run `certify revoke --reason ...` immediately for compromise, unsafe behavior or invalidated
   evidence. Revocation is append-only and blocks the execution gateway.

If local disk reserve blocks the build, run `make docker-inspect` first. Cleanup requires the
explicit command `PROJECT_DOCKER_APPLY=1 make docker-clean` and is restricted to resources carrying
the fixed `com.docker.compose.project=computeweaver` label. Global `docker system prune`, unrelated
volumes and other projects' images are outside this procedure.

Database rollback uses forward corrective migrations; applied migration files are checksum-locked
and never edited. Destructive down migrations are prohibited during incident recovery.
