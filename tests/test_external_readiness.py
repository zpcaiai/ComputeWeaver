from __future__ import annotations

import json
from pathlib import Path

from packages.certification.evidence import write_evidence
from packages.certification.readiness import evaluate_external_readiness


def _manifests(root: Path, revision: str) -> None:
    for batch in range(1, 21):
        directory = root / f"B{batch:02d}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "manifest.json").write_text(
            json.dumps({"batch": f"B{batch:02d}", "git_commit": revision, "mandatory_gates": {}}),
            encoding="utf-8",
        )


def test_external_readiness_exposes_every_fail_closed_next_action(tmp_path: Path) -> None:
    revision = "a" * 40
    _manifests(tmp_path, revision)
    report = evaluate_external_readiness(tmp_path, release_id="release-one", source_revision=revision)
    assert report.status != "PASS"
    by_name = {check.name: check for check in report.checks}
    assert by_name["evidence_request"].status == "NOT_RUN"
    assert all(by_name[name].next_action for name in (
        "security",
        "performance",
        "backup_restore",
        "external_integrations",
        "acceptance",
    ))


def test_external_readiness_rejects_tampered_preflight(tmp_path: Path) -> None:
    revision = "a" * 40
    _manifests(tmp_path, revision)
    path = tmp_path / "B20" / "production-preflight.json"
    write_evidence(
        path,
        {"status": "PASS", "source_revision": revision},
        command="preflight fixture",
        suite_name="preflight",
    )
    path.write_text(path.read_text(encoding="utf-8").replace('"status":"PASS"', '"status":"FAIL"'))
    report = evaluate_external_readiness(tmp_path, release_id="release-one", source_revision=revision)
    preflight = next(check for check in report.checks if check.name == "production_preflight")
    assert preflight.status == "FAIL"
    assert "SHA-256" in str(preflight.reason)
