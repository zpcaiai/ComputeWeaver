from __future__ import annotations

import subprocess

from scripts import project_docker


def test_project_docker_cleanup_is_dry_run_and_project_scoped(monkeypatch) -> None:
    calls: list[list[str]] = []

    def run(arguments: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(project_docker, "_run", run)
    result = project_docker.clean_project_resources(apply=False)
    assert result["status"] == "NOT_RUN"
    assert result["external_projects_affected"] is False
    planned = result["planned_command"]
    assert planned[:4] == ["docker", "compose", "-p", "computeweaver"]
    assert "down" not in {call[0] for call in calls}


def test_project_docker_apply_never_uses_global_prune(monkeypatch) -> None:
    calls: list[list[str]] = []

    def run(arguments: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(project_docker, "_run", run)
    result = project_docker.clean_project_resources(apply=True)
    assert result["status"] == "PASS"
    cleanup = next(call for call in calls if "down" in call)
    assert cleanup[:4] == ["compose", "-p", "computeweaver", "-f"]
    assert "--rmi" in cleanup and "local" in cleanup
    assert all("prune" not in call for command in calls for call in command)
