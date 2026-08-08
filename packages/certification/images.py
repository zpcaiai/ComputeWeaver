from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

IMAGE_REFERENCE = re.compile(r"^\S+@sha256:([0-9a-f]{64})$")
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ImageBundleEntry:
    reference: str
    archive: Path
    archive_sha256: str
    expected_image_id: str | None
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ImageBundle:
    manifest: Path
    manifest_sha256: str
    entries: tuple[ImageBundleEntry, ...]
    required_free_bytes: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_image_bundle(manifest_path: Path, *, available_bytes: int | None = None) -> ImageBundle:
    """Verify a portable docker-save bundle before any Docker state is changed."""

    manifest = manifest_path.resolve()
    document = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != "1.0.0":
        raise ValueError("image bundle manifest schema_version must be 1.0.0")
    raw_entries = document.get("images")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError("image bundle manifest must contain at least one image")
    reserve_gib = float(document.get("minimum_free_gib", 8))
    expansion_factor = float(document.get("expansion_factor", 2))
    if reserve_gib < 0 or expansion_factor < 1:
        raise ValueError("image bundle disk requirements are invalid")

    entries: list[ImageBundleEntry] = []
    references: set[str] = set()
    bundle_root = manifest.parent
    for item in raw_entries:
        if not isinstance(item, dict):
            raise ValueError("image bundle entries must be JSON objects")
        reference = str(item.get("reference", ""))
        if not IMAGE_REFERENCE.fullmatch(reference):
            raise ValueError(f"image is not pinned by SHA-256 digest: {reference}")
        if reference in references:
            raise ValueError(f"duplicate image reference: {reference}")
        references.add(reference)
        archive_value = str(item.get("archive", ""))
        archive_relative = Path(archive_value)
        if not archive_value or archive_relative.is_absolute():
            raise ValueError("image archive paths must be relative to the bundle manifest")
        archive = (bundle_root / archive_relative).resolve()
        try:
            archive.relative_to(bundle_root)
        except ValueError as error:
            raise ValueError("image archive escapes the bundle directory") from error
        if not archive.is_file():
            raise FileNotFoundError(archive)
        expected_archive_sha256 = str(item.get("archive_sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", expected_archive_sha256):
            raise ValueError("image archive SHA-256 is invalid")
        if _sha256(archive) != expected_archive_sha256:
            raise ValueError(f"image archive SHA-256 mismatch: {archive_relative}")
        expected_image_id = str(item["image_id"]) if item.get("image_id") else None
        if expected_image_id and not IMAGE_ID.fullmatch(expected_image_id):
            raise ValueError("expected Docker image ID is invalid")
        entries.append(
            ImageBundleEntry(
                reference,
                archive,
                expected_archive_sha256,
                expected_image_id,
                archive.stat().st_size,
            )
        )

    required = int(reserve_gib * 1024**3 + sum(entry.size_bytes for entry in entries) * expansion_factor)
    free = available_bytes if available_bytes is not None else shutil.disk_usage(bundle_root).free
    if free < required:
        raise OSError(f"insufficient disk for verified image bundle: required={required} available={free}")
    return ImageBundle(manifest, _sha256(manifest), tuple(entries), required)


def _run_docker(executable: str, arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - resolved Docker executable and constrained arguments
        [executable, *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def load_image_bundle(manifest_path: Path) -> dict[str, Any]:
    """Load a verified bundle and prove the resulting images retain their pinned digests."""

    try:
        bundle = verify_image_bundle(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {"status": "FAIL", "reason": str(error), "manifest": str(manifest_path), "results": []}
    executable = shutil.which("docker")
    if not executable:
        return {
            "status": "NOT_RUN",
            "reason": "Docker CLI is unavailable",
            "manifest": str(bundle.manifest),
            "manifest_sha256": bundle.manifest_sha256,
            "results": [],
        }
    results: list[dict[str, object]] = []
    for entry in bundle.entries:
        try:
            loaded = _run_docker(executable, ["image", "load", "--input", str(entry.archive)], 600)
        except subprocess.TimeoutExpired:
            results.append({"reference": entry.reference, "status": "FAIL", "reason": "docker image load timed out"})
            break
        if loaded.returncode != 0:
            results.append(
                {"reference": entry.reference, "status": "FAIL", "reason": "docker image load failed"}
            )
            break
        inspected = _run_docker(
            executable,
            ["image", "inspect", entry.reference, "--format", "{{json .RepoDigests}}|{{.Id}}"],
            30,
        )
        repo_digests: list[str] = []
        image_id = ""
        if inspected.returncode == 0 and "|" in inspected.stdout:
            raw_digests, image_id = inspected.stdout.strip().split("|", maxsplit=1)
            try:
                parsed = json.loads(raw_digests)
                if isinstance(parsed, list):
                    repo_digests = [str(item) for item in parsed]
            except json.JSONDecodeError:
                repo_digests = []
        expected_digest = entry.reference.rsplit("@", maxsplit=1)[1]
        digest_verified = any(value.rsplit("@", maxsplit=1)[-1] == expected_digest for value in repo_digests)
        id_verified = entry.expected_image_id is None or image_id == entry.expected_image_id
        passed = inspected.returncode == 0 and digest_verified and id_verified
        results.append(
            {
                "reference": entry.reference,
                "archive": entry.archive.name,
                "archive_sha256": entry.archive_sha256,
                "image_id": image_id or None,
                "status": "PASS" if passed else "FAIL",
                "reason": None if passed else "loaded image digest or image ID does not match the bundle manifest",
            }
        )
        if not passed:
            break
    all_loaded = len(results) == len(bundle.entries) and all(item["status"] == "PASS" for item in results)
    status = "PASS" if all_loaded else "FAIL"
    return {
        "status": status,
        "reason": None if status == "PASS" else "one or more bundled images failed verification",
        "manifest": str(bundle.manifest),
        "manifest_sha256": bundle.manifest_sha256,
        "required_free_bytes": bundle.required_free_bytes,
        "results": results,
    }


def export_image_bundle(
    references: list[str],
    destination: Path,
    *,
    minimum_free_gib: float = 8,
) -> Path:
    """Pull immutable references and export a portable, self-verifying docker-save bundle."""

    if not references or any(not IMAGE_REFERENCE.fullmatch(reference) for reference in references):
        raise ValueError("every exported image reference must be pinned by SHA-256 digest")
    if len(set(references)) != len(references):
        raise ValueError("export image references must be unique")
    if minimum_free_gib < 0:
        raise ValueError("minimum_free_gib cannot be negative")
    executable = shutil.which("docker")
    if not executable:
        raise RuntimeError("Docker CLI is unavailable")
    target = destination.resolve()
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise FileExistsError("image bundle export directory must be empty")

    inspected_images: list[tuple[str, str, int]] = []
    for reference in references:
        pulled = _run_docker(executable, ["image", "pull", reference], 900)
        if pulled.returncode != 0:
            raise RuntimeError(f"failed to pull immutable image reference: {reference}")
        inspected = _run_docker(
            executable,
            ["image", "inspect", reference, "--format", "{{.Id}}|{{.Size}}|{{json .RepoDigests}}"],
            30,
        )
        if inspected.returncode != 0 or inspected.stdout.count("|") < 2:
            raise RuntimeError(f"failed to inspect immutable image reference: {reference}")
        image_id, raw_size, raw_digests = inspected.stdout.strip().split("|", maxsplit=2)
        try:
            repo_digests = json.loads(raw_digests)
            size = int(raw_size)
        except (json.JSONDecodeError, ValueError) as error:
            raise RuntimeError("Docker image inspection returned invalid metadata") from error
        expected_digest = reference.rsplit("@", maxsplit=1)[1]
        if (
            not IMAGE_ID.fullmatch(image_id)
            or size <= 0
            or not isinstance(repo_digests, list)
            or not any(str(item).rsplit("@", maxsplit=1)[-1] == expected_digest for item in repo_digests)
        ):
            raise RuntimeError(f"pulled image does not retain the requested digest: {reference}")
        inspected_images.append((reference, image_id, size))

    required = int(minimum_free_gib * 1024**3 + sum(item[2] for item in inspected_images) * 1.2)
    if shutil.disk_usage(target).free < required:
        raise OSError("insufficient disk to export the immutable image bundle")
    manifest_entries: list[dict[str, object]] = []
    for index, (reference, image_id, _size) in enumerate(inspected_images, start=1):
        digest_prefix = reference.rsplit(":", maxsplit=1)[1][:12]
        archive = target / f"image-{index:02d}-{digest_prefix}.tar"
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{archive.name}.", dir=target)
        os.close(descriptor)
        Path(temporary_name).unlink()
        try:
            saved = _run_docker(executable, ["image", "save", "--output", temporary_name, reference], 1800)
            if saved.returncode != 0:
                raise RuntimeError(f"failed to export immutable image reference: {reference}")
            temporary = Path(temporary_name)
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise RuntimeError("Docker produced an empty image archive")
            temporary.replace(archive)
        finally:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass
        manifest_entries.append(
            {
                "reference": reference,
                "archive": archive.name,
                "archive_sha256": _sha256(archive),
                "image_id": image_id,
            }
        )
    manifest = target / "image-bundle.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "minimum_free_gib": minimum_free_gib,
                "expansion_factor": 2,
                "images": manifest_entries,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest


def bundle_document(bundle: ImageBundle) -> dict[str, object]:
    return {
        "manifest": str(bundle.manifest),
        "manifest_sha256": bundle.manifest_sha256,
        "required_free_bytes": bundle.required_free_bytes,
        "images": [
            {
                **asdict(entry),
                "archive": entry.archive.name,
            }
            for entry in bundle.entries
        ],
    }
