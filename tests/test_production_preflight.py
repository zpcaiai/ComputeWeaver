from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from packages.certification.preflight import run_preflight
from packages.certification.source import inspect_git_source, source_revision
from packages.secrets import CredentialResolver

GIT = shutil.which("git") or "/usr/bin/git"


def _git(root: Path, *arguments: str) -> None:
    subprocess.run([GIT, "-C", str(root), *arguments], check=True, capture_output=True, text=True)  # noqa: S603


def _committed_repository(root: Path) -> str:
    _git(root, "init", "-b", "main")
    (root / ".gitignore").write_text("generated.json\n", encoding="utf-8")
    (root / "source.txt").write_text("release source\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=ComputeWeaver Test",
        "-c",
        "user.email=test@computeweaver.invalid",
        "commit",
        "-m",
        "fixture",
    )
    return subprocess.run(  # noqa: S603
        [GIT, "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_source_binding_requires_exact_clean_git_worktree(tmp_path: Path) -> None:
    revision = _committed_repository(tmp_path)
    binding = inspect_git_source(tmp_path)
    assert binding.status == "PASS"
    assert binding.commit == revision
    assert binding.tree
    assert source_revision(tmp_path)[:2] == (revision, True)

    (tmp_path / "untracked.txt").write_text("not committed\n", encoding="utf-8")
    dirty = inspect_git_source(tmp_path)
    assert dirty.status == "FAIL"
    assert dirty.clean is False
    assert source_revision(tmp_path)[0].startswith("UNVERSIONED-SOURCE-")


def test_production_preflight_validates_real_inputs_without_returning_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    _committed_repository(tmp_path)
    production_config = tmp_path / "generated.json"
    production_config.write_text(json.dumps({"tenant": "tenant-one", "enabled": True}), encoding="utf-8")
    monkeypatch.setenv("COMPUTEWEAVER_CONNECTOR_SECRET_TOKEN_ONE", "super-secret-value")
    configuration = {
        "minimum_free_gib": 0,
        "check_docker": False,
        "required_tools": [],
        "images": ["registry.company.test/computeweaver@sha256:" + "a" * 64],
        "required_configs": [str(production_config)],
        "external_urls": ["https://control.company.test/health"],
        "credential_refs": ["secret://TOKEN_ONE"],
    }
    report = run_preflight(tmp_path, configuration, resolver=CredentialResolver(tmp_path / "secrets"))
    assert report.status == "PASS"
    assert "super-secret-value" not in json.dumps(report.as_document())

    production_config.write_text(json.dumps({"tenant": "REPLACE_WITH_TENANT"}), encoding="utf-8")
    failed = run_preflight(tmp_path, configuration, resolver=CredentialResolver(tmp_path / "secrets"))
    assert failed.status == "FAIL"
    assert next(check for check in failed.checks if check.name == "production_configs").status == "FAIL"
