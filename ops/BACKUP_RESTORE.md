# Backup and restore procedure

## Ownership and targets

- Operations owns scheduling, encrypted storage, retention and restore execution.
- Security owns key access and evidence review. Product owns the approved RPO/RTO.
- PostgreSQL target: continuous WAL or managed PITR plus an encrypted logical backup every five
  minutes. Keep 35 daily and 12 monthly restore points.
- Object storage target: versioning, object lock for release evidence, cross-account replication
  and lifecycle retention no shorter than the database retention.

## Backup gate

1. Configure the pinned CronJob image, database Secret, encrypted backup PVC and textfile metrics
   volume in `deploy/kubernetes/postgres-backup.yaml`.
2. Keep database URLs in Secrets. Never place credentials in a ConfigMap, command argument or log.
3. Alert when the last successful backup is older than five minutes or replication lags ten
   minutes. Failed jobs page operations and block release.
4. Record source database identity, start/end time, tool version, byte size and SHA-256 in the
   immutable backup manifest.

## Restore rehearsal

1. Disable external writes and restore to isolated PostgreSQL and object-storage targets.
2. Execute `make restore-rehearsal` with the release-bound evidence request.
3. Verify migration checksums, RLS, tenant/audit chains, object version IDs and SHA-256 readback.
4. Run normal-day plus all ten disturbance scenarios before declaring the restored target usable.
5. Destroy isolated targets only after evidence upload and independent review.

The RPO is five minutes and RTO is thirty minutes. A report from a synthetic/local target cannot
satisfy the production backup/restore gate.
