from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from packages.certification import images


def _bundle(tmp_path: Path) -> tuple[Path, str]:
    archive = tmp_path / "computeweaver.tar"
    archive.write_bytes(b"portable docker archive fixture")
    reference = "registry.company.test/computeweaver@sha256:" + "a" * 64
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "minimum_free_gib": 0,
                "expansion_factor": 1,
                "images": [
                    {
                        "reference": reference,
                        "archive": archive.name,
                        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest, reference


def test_image_bundle_rejects_tampering_and_unsafe_paths(tmp_path: Path) -> None:
    manifest, _ = _bundle(tmp_path)
    verified = images.verify_image_bundle(manifest, available_bytes=10_000)
    assert len(verified.entries) == 1
    verified.entries[0].archive.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="mismatch"):
        images.verify_image_bundle(manifest, available_bytes=10_000)

    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["images"][0]["archive"] = "../escape.tar"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        images.verify_image_bundle(manifest, available_bytes=10_000)


def test_image_bundle_load_revalidates_repo_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest, reference = _bundle(tmp_path)
    monkeypatch.setattr(images.shutil, "which", lambda _name: "/usr/local/bin/docker")

    def fake_run(
        command: list[str], *, text: bool, capture_output: bool, check: bool, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        assert text and capture_output and not check and timeout > 0
        if command[1:3] == ["image", "load"]:
            return subprocess.CompletedProcess(command, 0, "Loaded image\n", "")
        return subprocess.CompletedProcess(command, 0, json.dumps([reference]) + "|sha256:" + "b" * 64, "")

    monkeypatch.setattr(images.subprocess, "run", fake_run)
    result = images.load_image_bundle(manifest)
    assert result["status"] == "PASS"
    assert result["results"][0]["archive_sha256"]


def test_image_bundle_load_fails_when_digest_is_not_retained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest, _ = _bundle(tmp_path)
    monkeypatch.setattr(images.shutil, "which", lambda _name: "/usr/local/bin/docker")
    monkeypatch.setattr(
        images.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            "Loaded\n" if command[1:3] == ["image", "load"] else "[]|sha256:" + "b" * 64,
            "",
        ),
    )
    result = images.load_image_bundle(manifest)
    assert result["status"] == "FAIL"


def test_image_bundle_export_pulls_inspects_and_writes_verified_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = "registry.company.test/computeweaver@sha256:" + "c" * 64
    monkeypatch.setattr(images.shutil, "which", lambda _name: "/usr/local/bin/docker")

    def fake_run(
        executable: str, arguments: list[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        assert executable.endswith("docker") and timeout > 0
        command = [executable, *arguments]
        if arguments[1] == "inspect":
            metadata = "sha256:" + "d" * 64 + "|1024|" + json.dumps([reference])
            return subprocess.CompletedProcess(command, 0, metadata, "")
        if arguments[1] == "save":
            Path(arguments[arguments.index("--output") + 1]).write_bytes(b"docker-save-archive")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(images, "_run_docker", fake_run)
    manifest = images.export_image_bundle([reference], tmp_path / "bundle", minimum_free_gib=0)
    verified = images.verify_image_bundle(manifest, available_bytes=10_000)
    assert verified.entries[0].reference == reference
    assert verified.entries[0].expected_image_id == "sha256:" + "d" * 64
