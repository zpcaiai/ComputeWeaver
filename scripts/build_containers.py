from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TextIO, cast

from packages.certification.images import load_image_bundle

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "deploy" / "compose" / "docker-compose.yml"
DIRECT_NODE = "node:22.18.0-bookworm-slim@sha256:752ea8a2f758c34002a0461bd9f1cee4f9a3c36d48494586f60ffce1fc708e0e"
DIRECT_PYTHON = "python:3.12.11-slim@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f"
MIRROR_NODE = "mirror.gcr.io/library/" + DIRECT_NODE
MIRROR_PYTHON = "mirror.gcr.io/library/" + DIRECT_PYTHON
UNAVAILABLE_MARKERS = (
    "unexpected eof",
    "i/o timeout",
    "context deadline exceeded",
    "tls handshake timeout",
    "temporary failure",
    "no such host",
    "connection refused",
    "connection reset",
    "econnreset",
    "network is unreachable",
    "request canceled",
    "client.timeout",
    "operation timed out",
    "timed out",
)


@dataclass(frozen=True, slots=True)
class Attempt:
    registry: str
    status: str
    exit_code: int | None
    elapsed_seconds: float
    reason: str | None
    output: str


def classify_failure(output: str, *, timed_out: bool = False) -> tuple[str, str]:
    normalized = output.lower()
    if timed_out or any(marker in normalized for marker in UNAVAILABLE_MARKERS):
        return "NOT_RUN", "container registry or Docker daemon was unavailable"
    return "FAIL", "container image build failed"


def _run(command: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - command is assembled from fixed Docker verbs and image references
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _run_with_progress_timeout(
    command: list[str], *, inactivity_seconds: int, max_total_seconds: int
) -> tuple[subprocess.CompletedProcess[str], bool, str | None]:
    process = subprocess.Popen(  # noqa: S603 - fixed Docker command assembled by build_images
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        start_new_session=True,
    )
    if process.stdout is None:
        _stop(process)
        raise RuntimeError("Docker build output pipe is unavailable")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output: list[str] = []
    started = time.monotonic()
    last_progress = started
    timed_out = False
    timeout_reason: str | None = None
    try:
        while process.poll() is None:
            events = selector.select(timeout=1)
            for key, _ in events:
                line = cast(TextIO, key.fileobj).readline()
                if line:
                    output.append(line)
                    last_progress = time.monotonic()
            now = time.monotonic()
            if now - started >= max_total_seconds:
                timed_out = True
                timeout_reason = f"maximum build duration exceeded ({max_total_seconds}s)"
                _stop(process)
                break
            if now - last_progress >= inactivity_seconds:
                timed_out = True
                timeout_reason = f"build made no observable progress for {inactivity_seconds}s"
                _stop(process)
                break
        output.append(process.stdout.read())
    finally:
        selector.close()
        process.stdout.close()
    return (
        subprocess.CompletedProcess(command, process.returncode or 0, "".join(output), ""),
        timed_out,
        timeout_reason,
    )


def _profile(name: str) -> tuple[str, str]:
    if name == "mirror":
        return (
            os.getenv("COMPUTEWEAVER_NODE_BASE_MIRROR", MIRROR_NODE),
            os.getenv("COMPUTEWEAVER_PYTHON_BASE_MIRROR", MIRROR_PYTHON),
        )
    return (
        os.getenv("COMPUTEWEAVER_NODE_BASE", DIRECT_NODE),
        os.getenv("COMPUTEWEAVER_PYTHON_BASE", DIRECT_PYTHON),
    )


def build_images(*, timeout_seconds: int = 120, max_total_seconds: int = 900) -> tuple[str, list[Attempt]]:
    try:
        daemon = _run(["docker", "info", "--format", "{{.ServerVersion}}"], 15)
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return "NOT_RUN", [Attempt("daemon", "NOT_RUN", None, 0.0, "Docker daemon unavailable", str(error))]
    if daemon.returncode != 0:
        output = daemon.stdout + daemon.stderr
        return "NOT_RUN", [Attempt("daemon", "NOT_RUN", daemon.returncode, 0.0, "Docker daemon unavailable", output)]

    attempts: list[Attempt] = []
    for registry in ("direct", "mirror"):
        node_base, python_base = _profile(registry)
        command = [
            "docker",
            "compose",
            "--progress",
            "plain",
            "-f",
            str(COMPOSE_FILE),
            "build",
            "--build-arg",
            f"NODE_BASE={node_base}",
            "--build-arg",
            f"PYTHON_BASE={python_base}",
            "api",
        ]
        started = time.monotonic()
        try:
            process, timed_out, timeout_reason = _run_with_progress_timeout(
                command,
                inactivity_seconds=timeout_seconds,
                max_total_seconds=max_total_seconds,
            )
            elapsed = time.monotonic() - started
            output = process.stdout + process.stderr
            if process.returncode == 0 and not timed_out:
                attempts.append(Attempt(registry, "PASS", 0, elapsed, None, output))
                return "PASS", attempts
            status, reason = classify_failure(output, timed_out=timed_out)
            reason = timeout_reason or reason
            attempts.append(Attempt(registry, status, process.returncode, elapsed, reason, output))
            if status == "FAIL":
                return "FAIL", attempts
        except subprocess.TimeoutExpired as error:
            elapsed = time.monotonic() - started
            captured = "".join(
                value.decode(errors="replace") if isinstance(value, bytes) else value or ""
                for value in (error.stdout, error.stderr)
            )
            status, reason = classify_failure(captured, timed_out=True)
            attempts.append(Attempt(registry, status, None, elapsed, reason, captured))
    return "NOT_RUN", attempts


def image_id(service: str = "api") -> str | None:
    try:
        process = _run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "images", "-q", service],
            15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    value = process.stdout.strip().splitlines()
    if process.returncode == 0 and value:
        return value[0]
    candidates = (
        os.getenv("COMPUTEWEAVER_LOCAL_IMAGE", "computeweaver:local"),
        "compose-api:latest",
    )
    for candidate in candidates:
        try:
            inspected = _run(["docker", "image", "inspect", candidate, "--format", "{{.Id}}"], 15)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if inspected.returncode == 0 and inspected.stdout.strip():
            return inspected.stdout.strip()
    return None


def runtime_smoke(image: str) -> dict[str, Any]:
    command = [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m",  # noqa: S108 - container tmpfs, not a host temporary path
        image,
        "python",
        "-c",
        (
            "import os,pathlib; from apps.api.main import app; "
            "from packages.optimization.solvers import HighsSolver; "
            "import scripts.run_production_gates; "
            "uid=os.getuid(); paths=len(app.openapi()['paths']); static=pathlib.Path('/app/web/index.html').is_file(); "
            "assert uid not in (0,), 'root runtime'; assert paths>0; assert HighsSolver.available(); assert static; "
            "print(f'uid={uid} paths={paths} highs=true web_static={str(static).lower()}')"
        ),
    ]
    try:
        process = _run(command, 45)
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return {"status": "NOT_RUN", "observed": None, "reason": str(error)}
    return {
        "status": "PASS" if process.returncode == 0 else "FAIL",
        "observed": process.stdout.strip(),
        "reason": process.stderr.strip() or None if process.returncode != 0 else None,
    }


def run(
    *,
    timeout_seconds: int = 120,
    max_total_seconds: int = 900,
    image_bundle: Path | None = None,
) -> dict[str, Any]:
    bundle_result: dict[str, Any] | None = None
    if image_bundle is not None:
        bundle_result = load_image_bundle(image_bundle)
        if bundle_result["status"] != "PASS":
            return {
                "status": "FAIL",
                "attempts": [],
                "image_id": None,
                "runtime_smoke": {"status": "NOT_RUN", "observed": None, "reason": "image bundle failed"},
                "image_bundle": bundle_result,
            }
    status, attempts = build_images(timeout_seconds=timeout_seconds, max_total_seconds=max_total_seconds)
    identifier = image_id() if status == "PASS" else None
    smoke = (
        runtime_smoke(identifier)
        if identifier
        else {"status": "NOT_RUN", "observed": None, "reason": "image not built"}
    )
    result = {
        "status": status,
        "attempts": [asdict(attempt) for attempt in attempts],
        "image_id": identifier,
        "runtime_smoke": smoke,
    }
    if bundle_result is not None:
        result["image_bundle"] = bundle_result
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Build pinned images with registry fallback and hardened smoke")
    parser.add_argument("--timeout", type=int, default=120, help="per-registry inactivity timeout")
    parser.add_argument("--max-total", type=int, default=900, help="per-registry total build limit")
    parser.add_argument("--image-bundle", type=Path, help="verified offline docker-save bundle manifest")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    result = run(
        timeout_seconds=arguments.timeout,
        max_total_seconds=arguments.max_total,
        image_bundle=arguments.image_bundle,
    )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    summary = {
        **result,
        "attempts": [
            {key: value for key, value in attempt.items() if key != "output"} for attempt in result["attempts"]
        ],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    raise SystemExit(0 if result["status"] == "PASS" and result["runtime_smoke"]["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()
