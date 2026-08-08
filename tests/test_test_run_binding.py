from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from packages.certification.evidence import verify_evidence
from scripts import record_test_run

GIT = shutil.which("git") or "/usr/bin/git"


def _git(root: Path, *arguments: str) -> None:
    subprocess.run([GIT, "-C", str(root), *arguments], check=True, capture_output=True, text=True)  # noqa: S603


def test_test_run_binding_requires_clean_current_source(tmp_path: Path, monkeypatch) -> None:
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / ".gitignore").write_text("evidence/\n", encoding="utf-8")
    (tmp_path / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=ComputeWeaver Test",
        "-c",
        "user.email=test@computeweaver.invalid",
        "commit",
        "-m",
        "fixture",
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    junit = evidence / "test-results.xml"
    junit.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"/>', encoding="utf-8")
    coverage = evidence / "coverage.xml"
    coverage.write_text('<coverage line-rate="1"/>', encoding="utf-8")
    output = evidence / "test-run-binding.json"
    monkeypatch.setattr(record_test_run, "ROOT", tmp_path)

    passed = record_test_run.record_test_run(junit, output, coverage=coverage, suite_name="fixture-tests")
    assert passed["status"] == "PASS"
    assert verify_evidence(output)[0]

    (tmp_path / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
    failed = record_test_run.record_test_run(junit, output, coverage=coverage, suite_name="fixture-tests")
    assert failed["status"] == "FAIL"
