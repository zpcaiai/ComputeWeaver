from __future__ import annotations

import ipaddress
import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from packages.secrets import CredentialResolver

from .source import inspect_git_source

DIGEST_PIN = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
PLACEHOLDER_MARKERS = ("replace_with", "changeme", "todo", "example.com", "example.net", "example.org")


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    name: str
    status: str
    reason: str | None = None
    details: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class PreflightReport:
    status: str
    source_revision: str | None
    checks: tuple[PreflightCheck, ...]

    def as_document(self) -> dict[str, object]:
        return {
            "status": self.status,
            "source_revision": self.source_revision,
            "checks": [asdict(check) for check in self.checks],
        }


def _contains_placeholder(value: object) -> bool:
    if isinstance(value, str):
        normalized = value.lower()
        return any(marker in normalized for marker in PLACEHOLDER_MARKERS)
    if isinstance(value, dict):
        return any(_contains_placeholder(key) or _contains_placeholder(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_placeholder(item) for item in value)
    return False


def _production_https_url(value: str) -> bool:
    if _contains_placeholder(value):
        return False
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    hostname = parsed.hostname.lower()
    if hostname in {"localhost", "host.docker.internal"} or hostname.endswith((".local", ".localhost")):
        return False
    if any(marker in hostname for marker in ("example.com", "example.net", "example.org", ".invalid")):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return not (address.is_loopback or address.is_private or address.is_link_local or address.is_unspecified)


def _read_config(path: Path) -> tuple[bool, str | None]:
    if not path.is_file():
        return False, "required production configuration is missing"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "required production configuration is not valid JSON"
    if not isinstance(document, dict):
        return False, "required production configuration must be a JSON object"
    if _contains_placeholder(document):
        return False, "required production configuration contains placeholder values"
    return True, None


def _docker_check() -> PreflightCheck:
    executable = shutil.which("docker")
    if not executable:
        return PreflightCheck("docker_daemon", "FAIL", "Docker CLI is unavailable")
    try:
        result = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments
            [executable, "info", "--format", "{{.ServerVersion}}"],
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return PreflightCheck("docker_daemon", "FAIL", "Docker daemon probe timed out")
    if result.returncode != 0:
        return PreflightCheck("docker_daemon", "FAIL", "Docker daemon is unavailable")
    return PreflightCheck("docker_daemon", "PASS", details={"server_version": result.stdout.strip()})


def run_preflight(
    root: Path,
    configuration: dict[str, Any],
    *,
    resolver: CredentialResolver | None = None,
) -> PreflightReport:
    """Validate local prerequisites without mutating external systems or exposing secrets."""

    resolved_root = root.resolve()
    checks: list[PreflightCheck] = []
    binding = inspect_git_source(resolved_root)
    checks.append(
        PreflightCheck(
            "immutable_source",
            binding.status,
            binding.reason,
            {"commit": binding.commit, "tree": binding.tree, "clean": binding.clean},
        )
    )

    minimum_free_gib = float(configuration.get("minimum_free_gib", 8))
    if minimum_free_gib < 0:
        raise ValueError("minimum_free_gib cannot be negative")
    usage = shutil.disk_usage(resolved_root)
    available_gib = usage.free / (1024**3)
    checks.append(
        PreflightCheck(
            "disk_capacity",
            "PASS" if available_gib >= minimum_free_gib else "FAIL",
            None if available_gib >= minimum_free_gib else "free disk is below the configured production reserve",
            {"available_gib": round(available_gib, 2), "minimum_free_gib": minimum_free_gib},
        )
    )

    required_tools = configuration.get("required_tools", ["git", "docker", "pg_dump", "pg_restore"])
    if not isinstance(required_tools, list) or any(not isinstance(item, str) or not item for item in required_tools):
        raise ValueError("required_tools must be a list of executable names")
    missing_tools = sorted(tool for tool in required_tools if shutil.which(tool) is None)
    checks.append(
        PreflightCheck(
            "required_tools",
            "PASS" if not missing_tools else "FAIL",
            None if not missing_tools else "required production tools are unavailable",
            {"missing": missing_tools, "required": sorted(required_tools)},
        )
    )
    if configuration.get("check_docker", True) is True:
        checks.append(_docker_check())

    images = configuration.get("images", [])
    if not isinstance(images, list) or any(not isinstance(item, str) for item in images):
        raise ValueError("images must be a list of immutable image references")
    invalid_images = sorted(image for image in images if not DIGEST_PIN.fullmatch(image))
    checks.append(
        PreflightCheck(
            "image_pins",
            "PASS" if images and not invalid_images else "FAIL",
            None if images and not invalid_images else "all production images must be pinned by SHA-256 digest",
            {"count": len(images), "invalid": invalid_images},
        )
    )

    config_paths = configuration.get("required_configs", [])
    if not isinstance(config_paths, list) or any(not isinstance(item, str) for item in config_paths):
        raise ValueError("required_configs must be a list of paths")
    invalid_configs: list[dict[str, str]] = []
    for configured in config_paths:
        path = Path(configured)
        if not path.is_absolute():
            path = resolved_root / path
        valid, reason = _read_config(path)
        if not valid:
            invalid_configs.append({"path": configured, "reason": reason or "invalid"})
    checks.append(
        PreflightCheck(
            "production_configs",
            "PASS" if config_paths and not invalid_configs else "FAIL",
            None if config_paths and not invalid_configs else "production configuration is missing or unresolved",
            {"count": len(config_paths), "invalid": invalid_configs},
        )
    )

    urls = configuration.get("external_urls", [])
    if not isinstance(urls, list) or any(not isinstance(item, str) for item in urls):
        raise ValueError("external_urls must be a list")
    invalid_urls = sorted(url for url in urls if not _production_https_url(url))
    checks.append(
        PreflightCheck(
            "production_endpoints",
            "PASS" if urls and not invalid_urls else "FAIL",
            None if urls and not invalid_urls else "external endpoints must be non-local HTTPS URLs",
            {"count": len(urls), "invalid": invalid_urls},
        )
    )

    credential_refs = configuration.get("credential_refs", [])
    if not isinstance(credential_refs, list) or any(not isinstance(item, str) for item in credential_refs):
        raise ValueError("credential_refs must be a list")
    credential_resolver = resolver or CredentialResolver.from_env()
    unavailable_refs: list[str] = []
    for reference in credential_refs:
        try:
            credential_resolver.resolve(reference)
        except (OSError, PermissionError, ValueError):
            unavailable_refs.append(reference)
    checks.append(
        PreflightCheck(
            "credentials",
            "PASS" if credential_refs and not unavailable_refs else "FAIL",
            (
                None
                if credential_refs and not unavailable_refs
                else "one or more credential references cannot be resolved"
            ),
            {"count": len(credential_refs), "unavailable_refs": unavailable_refs},
        )
    )

    status = "PASS" if checks and all(check.status == "PASS" for check in checks) else "FAIL"
    return PreflightReport(status, binding.commit, tuple(checks))
