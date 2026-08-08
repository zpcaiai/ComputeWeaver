from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def canonical_json(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, default=str, separators=(",", ":"), sort_keys=True) + "\n").encode()


def content_sha256(document: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(document)).hexdigest()


def sidecar_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".sha256")


def junit_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".junit.xml")


def _atomic_write(path: Path, content: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def _checks(document: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("checks", "results"):
        value = document.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    gates = document.get("gates")
    if isinstance(gates, list):
        return [
            {
                "name": item.get("name", "gate"),
                "status": "PASS" if item.get("passed") is True else "FAIL",
                "reason": item.get("reason"),
            }
            for item in gates
            if isinstance(item, dict)
        ]
    status = str(document.get("status", "FAIL"))
    normalized = {
        "CERTIFIED": "PASS",
        "NOT_CERTIFIED": "FAIL",
        "PENDING_EXTERNAL_EVIDENCE": "NOT_RUN",
        "SIGNED": "PASS",
    }.get(status, status)
    return [{"name": "gate", "status": normalized}]


def write_junit(path: Path, document: dict[str, Any], *, suite_name: str) -> None:
    checks = _checks(document)
    failures = sum(item.get("status") == "FAIL" for item in checks)
    skipped = sum(item.get("status") == "NOT_RUN" for item in checks)
    suite = ET.Element(
        "testsuite",
        {
            "name": suite_name,
            "tests": str(len(checks)),
            "failures": str(failures),
            "errors": "0",
            "skipped": str(skipped),
        },
    )
    for index, item in enumerate(checks):
        name = str(item.get("name") or item.get("kind") or f"check-{index + 1}")
        case = ET.SubElement(suite, "testcase", {"classname": suite_name, "name": name})
        status = item.get("status")
        reason = str(item.get("reason") or "")
        if status == "FAIL":
            failure = ET.SubElement(case, "failure", {"message": reason or "gate failed"})
            failure.text = reason
        elif status == "NOT_RUN":
            ET.SubElement(case, "skipped", {"message": reason or "gate not run"})
    _atomic_write(path, ET.tostring(suite, encoding="utf-8", xml_declaration=True) + b"\n")


def write_evidence(
    path: Path,
    document: dict[str, Any],
    *,
    command: str,
    suite_name: str,
    generated_at: datetime | None = None,
) -> str:
    timestamp = generated_at or datetime.now(UTC)
    enriched = {
        **document,
        "batch": document.get("batch", "B20"),
        "git_commit": document.get("git_commit", document.get("source_revision")),
        "generated_at": document.get("generated_at", timestamp.isoformat()),
        "command": document.get("command", command),
        "result": document.get("result", document.get("status", "FAIL")),
        "tool_versions": document.get(
            "tool_versions",
            {"python": platform.python_version(), "computeweaver": "0.1.0"},
        ),
        "evidence_meta": {
            "schema_version": "1.0.0",
            "generated_at": timestamp.isoformat(),
            "command": command,
        },
    }
    content = canonical_json(enriched)
    digest = hashlib.sha256(content).hexdigest()
    _atomic_write(path, content)
    _atomic_write(sidecar_path(path), f"{digest}  {path.name}\n".encode())
    write_junit(junit_path(path), enriched, suite_name=suite_name)
    return digest


def verify_evidence(path: Path) -> tuple[bool, str | None]:
    sidecar = sidecar_path(path)
    if not path.is_file() or not sidecar.is_file():
        return False, "evidence document or SHA-256 sidecar is missing"
    try:
        expected, filename = sidecar.read_text(encoding="utf-8").strip().split(maxsplit=1)
        filename = filename.strip()
        if filename != path.name or len(expected) != 64:
            return False, "evidence SHA-256 sidecar is malformed"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if not hmac.compare_digest(actual, expected):
            return False, "evidence SHA-256 mismatch"
        document = json.loads(path.read_text(encoding="utf-8"))
        metadata = document.get("evidence_meta") if isinstance(document, dict) else None
        if not isinstance(metadata, dict) or metadata.get("schema_version") != "1.0.0":
            return False, "evidence metadata is missing"
    except (OSError, ValueError, json.JSONDecodeError):
        return False, "evidence document could not be parsed"
    return True, None
