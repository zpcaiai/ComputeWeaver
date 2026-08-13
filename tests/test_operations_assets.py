from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml


def test_ci_actions_are_pinned_to_node24_capable_release_commits() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    expected = {
        "checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
        "setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
        "setup-node": "820762786026740c76f36085b0efc47a31fe5020",
        "upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    }
    uses = re.findall(r"uses: actions/([^@]+)@([^\s#]+)", workflow)
    assert uses
    assert {name for name, _reference in uses} == set(expected)
    for name, reference in uses:
        assert reference == expected[name]
        assert re.fullmatch(r"[0-9a-f]{40}", reference)


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
    assert compose["name"] == "computeweaver"
    assert compose["services"]["simulator"]["profiles"] == ["simulation"]
    for service in ("api", "web"):
        healthcheck = compose["services"][service]["healthcheck"]
        assert healthcheck["start_period"] == "10s"
        assert healthcheck["timeout"] == "10s"


def test_web_supply_chain_uses_the_official_locked_registry() -> None:
    root = Path(__file__).resolve().parents[1]
    lock = json.loads((root / "apps/web/package-lock.json").read_text(encoding="utf-8"))
    resolved_hosts = {
        urlsplit(str(package["resolved"])).hostname
        for package in lock["packages"].values()
        if package.get("resolved")
    }
    assert resolved_hosts == {"registry.npmjs.org"}
    dockerfile = (root / "deploy/compose/Dockerfile").read_text(encoding="utf-8")
    assert (
        "npm ci --ignore-scripts --include=optional --registry=https://registry.npmjs.org"
        in dockerfile
    )


def test_api_deployment_can_execute_certification_on_read_only_root() -> None:
    root = Path(__file__).resolve().parents[1]
    documents = list(
        yaml.safe_load_all((root / "deploy/kubernetes/base.yaml").read_text(encoding="utf-8"))
    )
    config = next(document for document in documents if document.get("kind") == "ConfigMap")
    deployment = next(
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and document["metadata"]["name"] == "computeweaver-api"
    )
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
    volumes = {volume["name"]: volume for volume in pod["volumes"]}

    assert pod["securityContext"]["runAsUser"] == 65532
    assert pod["securityContext"]["runAsGroup"] == 65532
    assert pod["securityContext"]["fsGroup"] == 65532
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert mounts["release-evidence"]["mountPath"] == "/evidence"
    assert mounts["release-signing"]["readOnly"] is True
    assert volumes["release-evidence"]["persistentVolumeClaim"]["claimName"] == (
        "computeweaver-release-evidence"
    )
    assert volumes["release-signing"]["secret"]["secretName"] == (
        "computeweaver-release-signing"
    )
    assert volumes["release-signing"]["secret"]["defaultMode"] == 0o440
    assert config["data"]["COMPUTEWEAVER_CERTIFICATION_EVIDENCE_ROOT"] == "/evidence"
    assert config["data"]["COMPUTEWEAVER_RELEASE_ID"].startswith("REPLACE_")
    assert config["data"]["COMPUTEWEAVER_RELEASE_COMMIT"].startswith("REPLACE_")
    assert config["data"]["COMPUTEWEAVER_RELEASE_SIGNING_KEY_FILE"].startswith(
        "/var/run/secrets/"
    )
