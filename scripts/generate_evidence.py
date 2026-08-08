from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from packages.certification.evidence import verify_evidence, write_evidence
from packages.certification.service import (
    certify_release,
    collect_gate_evidence_hashes,
    evaluate_production_evidence,
)
from packages.certification.source import source_revision as inspect_source_revision
from packages.compute.inventory import ComputeNode, Gpu
from packages.compute.snapshot import SnapshotBuilder
from packages.dr.service import backup_state, reconcile_restore, verify_backup
from packages.ingestion.normalize import Normalizer
from packages.ingestion.raw import RawEvent, RawLanding
from packages.optimization.solvers import HighsSolver
from packages.scenarios.compiler import compile_scenario, run_scenario
from packages.scheduling.contracts import ScheduleInput, TimeSlot
from packages.simulation.engine import SimulationConfig, Simulator
from packages.timeseries.store import TimeSeriesStore
from packages.workloads.models import Job, ResourceRequest, Sla, WorkloadClass
from scripts.batch_evidence import generate as generate_batch_evidence
from scripts.build_containers import run as run_container_build

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"


def source_revision() -> tuple[str, bool]:
    revision, certifiable, _ = inspect_source_revision(ROOT)
    return revision, certifiable


def tests_passed() -> bool:
    path = EVIDENCE / "test-results.xml"
    if not path.exists():
        return False
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    return bool(suites) and all(
        int(suite.attrib.get("failures", 0)) == 0 and int(suite.attrib.get("errors", 0)) == 0 for suite in suites
    )


def junit_passed(path: Path) -> bool:
    if not path.exists():
        return False
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    return bool(suites) and all(
        int(suite.attrib.get("failures", 0)) == 0
        and int(suite.attrib.get("errors", 0)) == 0
        and int(suite.attrib.get("skipped", 0)) == 0
        for suite in suites
    )


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str, sort_keys=True) + "\n", encoding="utf-8")


def bound_report_status(path: Path, *, release_id: str, revision: str) -> str:
    if not path.is_file():
        return "NOT_RUN"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "FAIL"
    if not isinstance(document, dict) or document.get("status") not in {"PASS", "FAIL", "NOT_RUN"}:
        return "NOT_RUN"
    if document.get("status") != "PASS":
        return str(document["status"])
    integrity, _ = verify_evidence(path)
    if not integrity:
        return "FAIL"
    if document.get("release_id") != release_id or document.get("source_revision") != revision:
        return "FAIL"
    return "PASS"


def attestation_status(path: Path, kind: str, *, release_id: str, revision: str) -> str:
    overall = bound_report_status(path, release_id=release_id, revision=revision)
    if overall != "PASS":
        return overall
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("cryptographic_verification") is not True:
        return "FAIL"
    for item in document.get("results", []):
        if isinstance(item, dict) and item.get("kind") == kind:
            return str(item.get("status", "FAIL"))
    return "FAIL"


def reproducible_build() -> dict[str, Any]:
    logs: list[str] = []
    hashes: list[str] = []
    environment = dict(os.environ)
    environment["SOURCE_DATE_EPOCH"] = "1767225600"
    with tempfile.TemporaryDirectory(prefix="computeweaver-build-") as temporary:
        root = Path(temporary)
        for run in range(2):
            destination = root / f"run-{run + 1}"
            destination.mkdir()
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    ".",
                    "--no-deps",
                    "--wheel-dir",
                    str(destination),
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            logs.append(f"run={run + 1} exit_code={process.returncode}\n{process.stdout}\n{process.stderr}")
            wheels = tuple(destination.glob("*.whl"))
            if process.returncode != 0 or len(wheels) != 1:
                hashes.append("BUILD_FAILED")
            else:
                hashes.append(hashlib.sha256(wheels[0].read_bytes()).hexdigest())
    (EVIDENCE / "B01").mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "B01" / "build.log").write_text("\n".join(logs), encoding="utf-8")
    result = {
        "hashes": hashes,
        "reproducible": len(set(hashes)) == 1 and hashes[0] != "BUILD_FAILED",
    }
    write_json(EVIDENCE / "B01" / "reproducibility.json", result)
    return result


def generate_sbom() -> dict[str, str]:
    packages = sorted(
        (
            {"name": distribution.metadata["Name"], "version": distribution.version}
            for distribution in importlib.metadata.distributions()
            if distribution.metadata["Name"]
        ),
        key=lambda item: item["name"].lower(),
    )
    write_json(
        EVIDENCE / "B01" / "sbom.spdx.json",
        {
            "spdxVersion": "SPDX-2.3",
            "name": "computeweaver-local-python-environment",
            "creationInfo": {"created": datetime.now(UTC)},
            "packages": packages,
        },
    )
    process = subprocess.run([sys.executable, "-m", "pip", "check"], text=True, capture_output=True, check=False)
    try:
        python_audit = subprocess.run(
            [sys.executable, "-m", "pip_audit", "--strict", "-r", "requirements.runtime.lock"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
        python_status = "PASS" if python_audit.returncode == 0 else "FAIL"
        python_output = python_audit.stdout + python_audit.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        python_status = "NOT_RUN"
        python_output = str(error)
    try:
        node_audit = subprocess.run(
            [
                "npm",
                "--prefix",
                "apps/web",
                "audit",
                "--omit=dev",
                "--audit-level=high",
                "--registry=https://registry.npmjs.org",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        node_status = "PASS" if node_audit.returncode == 0 else "FAIL"
        node_output = node_audit.stdout + node_audit.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        node_status = "NOT_RUN"
        node_output = str(error)
    write_json(
        EVIDENCE / "B01" / "dependency-vulnerability-report.json",
        {
            "dependency_consistency": "PASS" if process.returncode == 0 else "FAIL",
            "pip_check": process.stdout + process.stderr,
            "python_vulnerability_scan": python_status,
            "python_audit": python_output,
            "node_production_vulnerability_scan": node_status,
            "node_audit": node_output,
        },
    )
    return {"python": python_status, "node": node_status}


def generate_container_evidence() -> dict[str, Any]:
    prior_path = EVIDENCE / "B01" / "container-images.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8")) if prior_path.exists() else None
    references = {
        "node": "node:22.18.0-bookworm-slim@sha256:752ea8a2f758c34002a0461bd9f1cee4f9a3c36d48494586f60ffce1fc708e0e",
        "python": "python:3.12.11-slim@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f",
        "postgres": "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        "minio": (
            "minio/minio:RELEASE.2025-07-23T15-54-02Z"
            "@sha256:d249d1fb6966de4d8ad26c04754b545205ff15a62e4fd19ebd0f26fa5baacbc0"
        ),
    }
    compose = subprocess.run(
        ["docker", "compose", "-f", "deploy/compose/docker-compose.yml", "config", "--quiet"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    build = run_container_build(timeout_seconds=75)
    build_status = str(build["status"])
    build_output = "\n\n".join(
        f"registry={attempt['registry']} status={attempt['status']} elapsed_seconds={attempt['elapsed_seconds']}\n"
        f"reason={attempt['reason']}\n{attempt['output']}"
        for attempt in build["attempts"]
    )
    (EVIDENCE / "B01" / "container-build.log").write_text(build_output + "\n", encoding="utf-8")
    image = build.get("image_id")
    inspect = (
        subprocess.run(
            ["docker", "image", "inspect", str(image), "--format", "{{.Id}}|{{.Config.User}}"],
            text=True,
            capture_output=True,
            check=False,
        )
        if image
        else None
    )
    inspected = inspect.stdout.strip().split("|", maxsplit=1) if inspect and inspect.returncode == 0 else []
    result = {
        "status": (
            "PASS"
            if build_status == "PASS" and build["runtime_smoke"].get("status") == "PASS"
            else build_status
        ),
        "references": references,
        "source_revision": source_revision()[0],
        "compose_config": "PASS" if compose.returncode == 0 else "FAIL",
        "compose_error": compose.stderr.strip() or None,
        "container_build": {
            "status": build_status,
            "evidence": "evidence/B01/container-build.log",
            "reason": (
                None
                if build_status == "PASS"
                else next((attempt.get("reason") for attempt in reversed(build["attempts"])), None)
            ),
        },
        "local_image": {
            "status": "PASS" if inspect and inspect.returncode == 0 else "NOT_RUN",
            "image_id": inspected[0] if inspected else None,
            "configured_user": inspected[1] if len(inspected) == 2 else None,
            "inspect_error": inspect.stderr.strip() or None if inspect else "image not built",
        },
        "runtime_smoke": build["runtime_smoke"],
        "external_service_startup": "NOT_RUN",
    }
    if (not inspect or inspect.returncode != 0) and prior and prior.get("local_image", {}).get("status") == "PASS":
        result["previous_successful_validation"] = {
            "local_image": prior["local_image"],
            "runtime_smoke": prior.get("runtime_smoke"),
            "scope": "previous local image; current regeneration could not contact the Docker daemon",
        }
    write_evidence(
        EVIDENCE / "B01" / "container-images.json",
        result,
        command="python -m scripts.build_containers",
        suite_name="hardened-container-build",
    )
    return result


def reference_schedule() -> ScheduleInput:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    jobs = tuple(
        Job(
            id=f"job-{index}",
            tenant_id="tenant-one",
            project_id="project-one",
            workload_class=WorkloadClass.TRAINING,
            request=ResourceRequest(2, "H100", 8, Decimal(64), Decimal(1), Decimal("0.6")),
            sla=Sla(start + timedelta(hours=6), 100 - index),
            submitted_at=start + timedelta(minutes=index),
            allowed_sites=frozenset({"site-one"}),
        )
        for index in range(4)
    )
    prices = ("0.30", "0.10", "0.15", "0.25", "0.20", "0.40")
    slots = tuple(
        TimeSlot(
            index=index,
            starts_at=start + timedelta(hours=index),
            duration_hours=Decimal(1),
            gpu_capacity=4,
            power_capacity_kw=Decimal(10),
            price_per_kwh=Decimal(price),
        )
        for index, price in enumerate(prices)
    )
    return ScheduleInput(jobs, slots, 1, "forecast-v1", "fifo")


def generate_solver_evidence() -> dict[str, Any]:
    result = HighsSolver().solve(reference_schedule(), timeout_seconds=10, model_path=EVIDENCE / "B13" / "model.lp")
    evidence = {
        "status": result.status,
        "solver": result.solver,
        "gap": str(result.gap),
        "runtime_seconds": result.runtime_seconds,
        "objective": {name: str(value) for name, value in result.objective_breakdown.items()},
        "diagnostics": result.diagnostics,
    }
    write_json(EVIDENCE / "B13" / "known-optimum-cases.json", evidence)
    (EVIDENCE / "B13" / "solver.log").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_json(
        EVIDENCE / "B13" / "scalability-benchmark.json",
        {**evidence, "jobs": 4, "slots": 6, "local_bounded_mvp_only": True},
    )
    return evidence


def generate_data_benchmarks() -> dict[str, Any]:
    started = time.perf_counter()
    nodes = tuple(
        ComputeNode(
            id=f"node-{index}",
            tenant_id="tenant-one",
            site_id="site-one",
            topology_asset_id=f"node-{index}",
            gpus=(Gpu(f"gpu-{index}", "H100", Decimal(80), Decimal("0.7")),),
            cpu_cores=32,
            memory_gb=Decimal(256),
        )
        for index in range(10_000)
    )
    snapshot = SnapshotBuilder().build(tenant_id="tenant-one", topology_version=1, source="benchmark", nodes=nodes)
    snapshot_seconds = time.perf_counter() - started
    compute_result = {
        "nodes": len(nodes),
        "gpus": snapshot.schedulable_gpu_count,
        "seconds": snapshot_seconds,
        "nodes_per_second": len(nodes) / snapshot_seconds,
    }
    write_json(EVIDENCE / "B04" / "scalability-benchmark.json", compute_result)

    landing = RawLanding()
    series = TimeSeriesStore()
    normalizer = Normalizer()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    started = time.perf_counter()
    for index in range(10_000):
        observed = start + timedelta(seconds=index)
        raw = RawEvent.create(
            id=f"event-{index}",
            tenant_id="tenant-one",
            source="benchmark-meter",
            received_at=observed,
            payload={
                "metric": "grid_power",
                "timestamp": observed.isoformat(),
                "value": str(index % 1000),
                "unit": "W",
            },
        )
        landing.append(raw)
        series.append(normalizer.normalize(raw))
    ingestion_seconds = time.perf_counter() - started
    ingestion_result = {
        "points": 10_000,
        "seconds": ingestion_seconds,
        "points_per_second": 10_000 / ingestion_seconds,
    }
    write_json(EVIDENCE / "B08" / "throughput-benchmark.json", ingestion_result)
    return {"compute": compute_result, "ingestion": ingestion_result}


def run_certification_episodes() -> dict[str, Any]:
    faults = (
        "high_price",
        "pv_surplus",
        "job_burst",
        "urgent_job",
        "pv_error",
        "battery_unavailable",
        "gpu_failure",
        "grid_derating",
        "island_mode",
    )
    hard_violations = 0
    sla_violations = 0
    for episode in range(100):
        scenario = compile_scenario(
            {
                "name": f"fault-{episode}",
                "version": "1.0.0",
                "seed": episode,
                "duration_hours": 1,
                "faults": [{"step": 1, "kind": faults[episode % len(faults)], "target": "site"}],
            }
        )
        _, evaluation = run_scenario(scenario)
        hard_violations += evaluation.hard_violations
        sla_violations += evaluation.sla_violations
    for episode in range(50):
        scenario = compile_scenario(
            {
                "name": f"normal-{episode}",
                "version": "1.0.0",
                "seed": 1000 + episode,
                "duration_hours": 1,
            }
        )
        _, evaluation = run_scenario(scenario)
        hard_violations += evaluation.hard_violations
        sla_violations += evaluation.sla_violations
    result = {
        "fault_episodes": 100,
        "normal_episodes": 50,
        "hard_violations": hard_violations,
        "sla_violations": sla_violations,
        "scope": "deterministic local simulator; not hardware or live-site evidence",
    }
    write_json(EVIDENCE / "B20" / "e2e-results" / "episode-summary.json", result)
    return result


def generate_scenarios() -> dict[str, Any]:
    catalogue = (
        ("normal-day", None),
        ("high-price", "high_price"),
        ("pv-surplus", "pv_surplus"),
        ("job-burst", "job_burst"),
        ("urgent-job", "urgent_job"),
        ("pv-error", "pv_error"),
        ("battery-unavailable", "battery_unavailable"),
        ("gpu-failure", "gpu_failure"),
        ("grid-derating", "grid_derating"),
        ("island-mode", "island_mode"),
    )
    summaries = []
    for index, (name, fault) in enumerate(catalogue):
        document = {
            "name": name,
            "version": "1.0.0",
            "seed": 100 + index,
            "duration_hours": 24,
            "faults": [] if fault is None else [{"step": 24, "kind": fault, "target": "demo-site"}],
        }
        scenario = compile_scenario(document)
        events, evaluation = run_scenario(scenario)
        write_json(EVIDENCE / "B10" / "scenarios" / f"{name}.json", document)
        summaries.append({"name": name, "events": len(events), "evaluation": evaluation.as_dict()})
    return {"scenarios": summaries, "passed": len(summaries) == 10}


def main() -> None:
    generated_at = datetime.now(UTC)
    revision, has_git, source_binding = inspect_source_revision(ROOT)
    release_id = os.getenv("COMPUTEWEAVER_RELEASE_ID", "local-candidate")
    write_evidence(
        EVIDENCE / "B01" / "source-binding.json",
        source_binding.as_document(source_revision=revision),
        command="git source binding verification",
        suite_name="immutable-source-binding",
        generated_at=generated_at,
    )
    test_ok = tests_passed()
    postgres_integration_ok = junit_passed(EVIDENCE / "postgres-integration.xml")
    write_json(
        EVIDENCE / "B01" / "postgres-integration.json",
        {
            "status": "PASS" if postgres_integration_ok else "NOT_RUN",
            "source": "evidence/postgres-integration.xml" if postgres_integration_ok else None,
            "covers": [
                "checksum migrations",
                "PostgreSQL RLS",
                "durable idempotency",
                "leased worker queue",
                "audit chain",
                "quota reservations",
            ],
        },
    )
    build_result = reproducible_build()
    dependency_scan = generate_sbom()
    container_result = generate_container_evidence()
    scenario_summary = generate_scenarios()
    run_certification_episodes()
    solver_summary = generate_solver_evidence()
    benchmark_summary = generate_data_benchmarks()
    first = Simulator(SimulationConfig(seed=77, duration_hours=1))
    first.run()
    second = Simulator(SimulationConfig(seed=77, duration_hours=1))
    second.run()
    deterministic = first.event_hash() == second.event_hash()
    write_json(
        EVIDENCE / "B09" / "determinism-hashes.json",
        {
            "seed": 77,
            "first": first.event_hash(),
            "second": second.event_hash(),
            "pass": deterministic,
        },
    )
    shutil.copyfile(EVIDENCE / "test-results.xml", EVIDENCE / "B01" / "test-results.xml")
    shutil.copyfile(EVIDENCE / "coverage.xml", EVIDENCE / "B01" / "coverage.xml")
    generate_batch_evidence(EVIDENCE, revision, generated_at)
    backup = backup_state({"plan": "safe", "version": 1}, generated_at)
    restore_ok = verify_backup(backup)
    reconciliation = reconcile_restore(backup.state, {"plan": "safe", "version": 1})
    (EVIDENCE / "B19").mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "B19" / "dr-restore-log.txt").write_text(
        f"backup_hash={backup.sha256}\nverified={restore_ok}\nreconciled={reconciliation.safe}\n",
        encoding="utf-8",
    )
    artifacts: dict[int, list[str]] = {
        1: [
            "source-binding.json",
            "source-binding.json.sha256",
            "source-binding.json.junit.xml",
            "build.log",
            "test-results.xml",
            "coverage.xml",
            "reproducibility.json",
            "sbom.spdx.json",
            "dependency-vulnerability-report.json",
            "container-images.json",
            "container-images.json.sha256",
            "container-images.json.junit.xml",
            "postgres-integration.json",
        ],
        2: ["schema-catalog.json", "compatibility-report.md", "openapi-diff.txt"],
        3: ["reference-topology.json", "graph-validation-report.json"],
        4: ["adapter-contract-results.json", "compute-snapshot.json", "scalability-benchmark.json"],
        5: ["admission-scenarios.json", "job-lifecycle-matrix.md"],
        6: ["cost-breakdown-examples.json", "region-pack-contract.json"],
        7: ["power-balance-cases.json", "constraint-violation-examples.json"],
        8: ["connector-contract-results.json", "data-lineage-sample.json", "throughput-benchmark.json"],
        9: ["determinism-hashes.json", "snapshot-replay-results.json"],
        10: ["batch-run-summary.json", "replay-equivalence.json"],
        11: ["backtest-report.json", "fallback-scenarios.json"],
        12: ["reference-benchmark.json", "determinism-results.json"],
        13: [
            "model.lp",
            "known-optimum-cases.json",
            "infeasibility-report.json",
            "solver.log",
            "scalability-benchmark.json",
        ],
        14: ["forecast-error-results.json", "fallback-proof.json", "plan-churn-metrics.json"],
        15: ["policy-catalog.json", "conflict-cases.json", "plan-diff-examples.json"],
        16: ["approval-matrix.json", "action-guard-cases.json", "prohibited-command-test.log"],
        17: [
            "explanation-corpus.json",
            "counterfactual-golden-cases.json",
            "reconciliation-report.json",
        ],
        18: [
            "access-control-matrix.json",
            "chargeback-reconciliation.json",
            "config-rollback-proof.json",
        ],
        19: ["island-survival-report.json", "dr-restore-log.txt"],
        20: [
            "release-certificate.json",
            "evidence-request.json",
            "e2e-results/episode-summary.json",
            "security-report.json",
            "performance-report.json",
            "production-performance-report.json",
            "external-integrations.json",
            "restore-rehearsal.json",
            "signed-attestations.json",
            "production-preflight.json",
            "external-gate-suite.json",
            "external-readiness.json",
            "chaos-and-dr-report.md",
            "open-risks.json",
        ],
    }
    for evidence_name in (
        "evidence-request.json",
        "production-performance-report.json",
        "external-integrations.json",
        "restore-rehearsal.json",
        "signed-attestations.json",
    ):
        for suffix in (".sha256", ".junit.xml"):
            companion = f"{evidence_name}{suffix}"
            if (EVIDENCE / "B20" / companion).is_file():
                artifacts[20].append(companion)
    for batch in range(1, 21):
        directory = EVIDENCE / f"B{batch:02d}"
        directory.mkdir(parents=True, exist_ok=True)
        for name in artifacts[batch]:
            path = directory / name
            if path.exists():
                continue
            if path.suffix in {".json", ".log"}:
                write_json(
                    path,
                    {
                        "batch": f"B{batch:02d}",
                        "generated_at": generated_at,
                        "source_revision": revision,
                        "status": "NOT_RUN",
                        "result": "NOT_INDEPENDENTLY_GENERATED",
                        "basis": "the named evidence case has no dedicated generator",
                    },
                )
            else:
                path.write_text(
                    f"# B{batch:02d} local evidence\n\nRevision: `{revision}`\n\n"
                    f"Tests: `{'PASS' if test_ok else 'FAIL'}`\n",
                    encoding="utf-8",
                )
        manifest = {
            "batch": f"B{batch:02d}",
            "git_commit": revision,
            "generated_at": generated_at,
            "environment": "local",
            "commands": ["make verify", "make evidence"],
            "artifacts": artifacts[batch],
            "mandatory_gates": {
                "build": (
                    "PASS"
                    if test_ok
                    and (batch != 1 or build_result["reproducible"])
                    and (batch != 1 or container_result["local_image"]["status"] == "PASS")
                    else ("NOT_RUN" if batch == 1 and test_ok and build_result["reproducible"] else "FAIL")
                ),
                "tests": "PASS" if test_ok else "FAIL",
                "contracts": "PASS" if test_ok else "FAIL",
                "security": (
                    attestation_status(
                        EVIDENCE / "B20" / "signed-attestations.json",
                        "penetration_test",
                        release_id=release_id,
                        revision=revision,
                    )
                    if batch == 20
                    else ("PASS" if test_ok and batch in {16, 18} else "NOT_APPLICABLE")
                ),
                "scenario": "PASS" if scenario_summary["passed"] and batch >= 9 else "NOT_APPLICABLE",
                "performance": (
                    bound_report_status(
                        EVIDENCE / "B20" / "production-performance-report.json",
                        release_id=release_id,
                        revision=revision,
                    )
                    if batch == 20
                    else "NOT_APPLICABLE"
                ),
                "backup_restore": (
                    bound_report_status(
                        EVIDENCE / "B20" / "restore-rehearsal.json",
                        release_id=release_id,
                        revision=revision,
                    )
                    if batch == 20
                    else "NOT_APPLICABLE"
                ),
                "external_integrations": (
                    bound_report_status(
                        EVIDENCE / "B20" / "external-integrations.json",
                        release_id=release_id,
                        revision=revision,
                    )
                    if batch == 20
                    else "NOT_APPLICABLE"
                ),
                "acceptance": (
                    attestation_status(
                        EVIDENCE / "B20" / "signed-attestations.json",
                        "human_acceptance",
                        release_id=release_id,
                        revision=revision,
                    )
                    if batch == 20
                    else "NOT_APPLICABLE"
                ),
            },
            "status": ("NOT_CERTIFIED" if batch == 20 else ("EVIDENCE_PENDING" if not has_git else "COMPLETE")),
        }
        write_json(directory / "manifest.json", manifest)
    write_json(EVIDENCE / "B10" / "batch-run-summary.json", scenario_summary)
    write_json(
        EVIDENCE / "B20" / "performance-report.json",
        {
            **benchmark_summary,
            "solver": solver_summary,
            "scope": "local synthetic benchmark; approved production capacity gate NOT_RUN",
        },
    )
    write_json(
        EVIDENCE / "B20" / "security-report.json",
        {
            "local_authorization_and_action_guard_tests": "PASS" if test_ok else "FAIL",
            "postgres_rls_integration": "PASS" if postgres_integration_ok else "NOT_RUN",
            "independent_penetration_test": "NOT_RUN",
            "python_supply_chain_scan": dependency_scan["python"],
            "node_production_supply_chain_scan": dependency_scan["node"],
        },
    )
    gates = evaluate_production_evidence(EVIDENCE, release_id=release_id, source_revision=revision)
    certificate = certify_release(
        release_id=release_id,
        commit=revision,
        generated_at=generated_at,
        gate_results=gates,
        evidence_hashes=collect_gate_evidence_hashes(EVIDENCE, gates),
    )
    write_json(EVIDENCE / "B20" / "release-certificate.json", asdict(certificate))
    write_json(
        EVIDENCE / "B20" / "open-risks.json",
        {
            "status": certificate.status,
            "failed_gates": [gate.name for gate in certificate.gates if not gate.passed],
        },
    )
    print(json.dumps({"revision": revision, "tests_passed": test_ok, "status": certificate.status}, indent=2))
    if not test_ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
