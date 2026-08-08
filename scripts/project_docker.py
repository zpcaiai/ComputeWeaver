from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from packages.certification.evidence import write_evidence

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "deploy" / "compose" / "docker-compose.yml"
PROJECT = "computeweaver"


@dataclass(frozen=True, slots=True)
class ProjectDockerInventory:
    status: str
    project: str
    compose_file: str
    available_bytes: int
    containers: tuple[str, ...]
    images: tuple[str, ...]
    volumes: tuple[str, ...]
    networks: tuple[str, ...]
    reason: str | None = None


def _run(arguments: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("docker")
    if not executable:
        raise FileNotFoundError("Docker CLI is unavailable")
    return subprocess.run(  # noqa: S603 - resolved Docker executable and fixed project-scoped verbs
        [executable, *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _lines(arguments: list[str]) -> tuple[str, ...]:
    result = _run(arguments)
    if result.returncode != 0:
        raise RuntimeError("Docker project inventory command failed")
    return tuple(line for line in result.stdout.splitlines() if line.strip())


def inspect_project_resources() -> ProjectDockerInventory:
    available = shutil.disk_usage(ROOT).free
    try:
        daemon = _run(["info", "--format", "{{.ServerVersion}}"], 15)
        compose = _run(["compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), "config", "--quiet"], 30)
        if daemon.returncode != 0 or compose.returncode != 0:
            raise RuntimeError("Docker daemon or ComputeWeaver Compose configuration is unavailable")
        label = f"label=com.docker.compose.project={PROJECT}"
        return ProjectDockerInventory(
            "PASS",
            PROJECT,
            str(COMPOSE_FILE),
            available,
            _lines(["ps", "-a", "--filter", label, "--format", "{{.ID}} {{.Names}} {{.Status}}"]),
            _lines(["image", "ls", "--filter", label, "--format", "{{.ID}} {{.Repository}}:{{.Tag}}"]),
            _lines(["volume", "ls", "--filter", label, "--format", "{{.Name}}"]),
            _lines(["network", "ls", "--filter", label, "--format", "{{.ID}} {{.Name}}"]),
        )
    except (FileNotFoundError, RuntimeError, subprocess.TimeoutExpired) as error:
        return ProjectDockerInventory("FAIL", PROJECT, str(COMPOSE_FILE), available, (), (), (), (), str(error))


def clean_project_resources(*, apply: bool) -> dict[str, Any]:
    """Remove only resources owned by the explicit ComputeWeaver Compose project."""

    before = inspect_project_resources()
    command = [
        "compose",
        "-p",
        PROJECT,
        "-f",
        str(COMPOSE_FILE),
        "down",
        "--remove-orphans",
        "--volumes",
        "--rmi",
        "local",
    ]
    if not apply:
        return {
            "status": "NOT_RUN",
            "project": PROJECT,
            "reason": "dry run only; pass --apply to remove project-owned resources",
            "planned_command": ["docker", *command],
            "before": asdict(before),
            "external_projects_affected": False,
        }
    if before.status != "PASS":
        return {
            "status": "FAIL",
            "project": PROJECT,
            "reason": before.reason,
            "before": asdict(before),
            "external_projects_affected": False,
        }
    try:
        result = _run(command, 300)
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return {
            "status": "FAIL",
            "project": PROJECT,
            "reason": str(error),
            "before": asdict(before),
            "external_projects_affected": False,
        }
    after = inspect_project_resources()
    reclaimed = max(0, after.available_bytes - before.available_bytes)
    passed = result.returncode == 0 and after.status == "PASS"
    return {
        "status": "PASS" if passed else "FAIL",
        "project": PROJECT,
        "reason": None if passed else "project-scoped Docker cleanup failed",
        "before": asdict(before),
        "after": asdict(after),
        "reclaimed_bytes": reclaimed,
        "external_projects_affected": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect or clean only ComputeWeaver-owned Docker resources")
    parser.add_argument("command", choices=("inspect", "clean"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("evidence/B01/project-docker-resources.json"))
    arguments = parser.parse_args()
    if arguments.command == "inspect":
        document: dict[str, Any] = asdict(inspect_project_resources())
    else:
        document = clean_project_resources(apply=arguments.apply)
    write_evidence(
        arguments.output,
        document,
        command=f"python -m scripts.project_docker {arguments.command}",
        suite_name="project-scoped-docker-resources",
    )
    print(json.dumps(document, indent=2, sort_keys=True))
    raise SystemExit(0 if document["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()
