# Operations

SLOs, alerts and runbooks in this directory are release inputs. The local profile is safe for
simulation and shadow evaluation. Production writes require B20 certification, explicit guarded
mode, a release certificate, role-separated approval and a passing Action Guard decision.

Operational release inputs:

- `SLOS.md`: measurable objectives and error-budget policy.
- `alerts.yaml`: deployable Prometheus alert rules mapped to the SLOs and runbooks.
- `RUNBOOKS.md`: component degradation and recovery procedures.
- `BACKUP_RESTORE.md`: backup ownership, retention, RPO/RTO and restore verification.
- `INCIDENT_RESPONSE.md`: severity, command roles, evidence preservation and closure.
- `RELEASE_ROLLBACK.md`: immutable release, rollback, certificate and revocation workflow.

The PostgreSQL backup CronJob template is in `deploy/kubernetes/postgres-backup.yaml`. It remains
suspended until the release operator replaces the image/PVC/Secret references and verifies a
restore rehearsal. A template or alert definition is never counted as successful production
evidence by B20.
