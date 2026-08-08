from __future__ import annotations

from pathlib import Path

import yaml


def test_operational_alerts_cover_slo_safety_certificate_and_restore() -> None:
    root = Path(__file__).resolve().parents[1]
    document = yaml.safe_load((root / "ops/alerts.yaml").read_text(encoding="utf-8"))
    alerts = {rule["alert"] for group in document["spec"]["groups"] for rule in group["rules"]}
    assert {
        "ComputeWeaverApiAvailabilityBurn",
        "ComputeWeaverReadLatencyHigh",
        "ComputeWeaverTelemetryAbsent",
        "ComputeWeaverDeadLetterJobs",
        "ComputeWeaverConnectorStale",
        "ComputeWeaverCertificateRevoked",
        "ComputeWeaverCertificateExpiring",
        "ComputeWeaverBackupStale",
        "ComputeWeaverRestoreRehearsalStale",
    } <= alerts
    for group in document["spec"]["groups"]:
        for rule in group["rules"]:
            assert rule["expr"] and rule["for"]
            assert rule["labels"]["severity"] in {"warning", "critical"}
            assert rule["labels"]["runbook"]


def test_backup_cronjob_is_pinned_hardened_and_operator_suspended() -> None:
    root = Path(__file__).resolve().parents[1]
    job = yaml.safe_load((root / "deploy/kubernetes/postgres-backup.yaml").read_text(encoding="utf-8"))
    assert job["kind"] == "CronJob"
    assert job["spec"]["schedule"] == "*/5 * * * *"
    assert job["spec"]["suspend"] is True
    pod = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    container = pod["containers"][0]
    assert "@sha256:" in container["image"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert "secretKeyRef" in container["env"][0]["valueFrom"]


def test_incident_release_and_backup_runbooks_define_fail_closed_authority() -> None:
    root = Path(__file__).resolve().parents[1]
    combined = "\n".join(
        (root / "ops" / name).read_text(encoding="utf-8")
        for name in ("BACKUP_RESTORE.md", "INCIDENT_RESPONSE.md", "RELEASE_ROLLBACK.md")
    )
    for phrase in (
        "RPO",
        "RTO",
        "revoke",
        "immutable",
        "external writes",
        "role-separated",
        "restore rehearsal",
    ):
        assert phrase.lower() in combined.lower()


def test_finite_simulator_is_not_a_default_long_running_compose_service() -> None:
    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "deploy/compose/docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["simulator"]["profiles"] == ["simulation"]
    for service in ("api", "web"):
        healthcheck = compose["services"][service]["healthcheck"]
        assert healthcheck["start_period"] == "10s"
        assert healthcheck["timeout"] == "10s"
