from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .attestations import verify_verification_material
from .evidence import verify_evidence
from .requests import IMMUTABLE_REVISION, load_verified_request
from .source import GIT_OBJECT_ID

MAX_XML_EVIDENCE_BYTES = 20 * 1024 * 1024


def _safe_xml_root(path: Path) -> ET.Element:
    content = path.read_bytes()
    if len(content) > MAX_XML_EVIDENCE_BYTES:
        raise ValueError("XML evidence exceeds the 20 MiB limit")
    upper = content.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("XML evidence cannot contain DTD or entity declarations")
    return ET.fromstring(content)  # noqa: S314 - bounded input with DTD/entity rejection


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    passed: bool
    evidence: tuple[str, ...]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CertificationResult:
    release_id: str
    commit: str
    generated_at: datetime
    expires_at: datetime
    status: str
    gates: tuple[GateResult, ...]
    risks: tuple[str, ...]
    evidence_hashes: dict[str, str]
    certificate_hash: str
    artifacts: dict[str, str]
    test_summary: dict[str, Any]
    scenario_metrics: dict[str, Any]
    approvals: tuple[dict[str, Any], ...]
    signature: str | None = None


MANDATORY_GATES = (
    "build",
    "tests",
    "contracts",
    "security",
    "scenarios",
    "performance",
    "backup_restore",
    "external_integrations",
    "acceptance",
)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def certify_release(
    *,
    release_id: str,
    commit: str,
    generated_at: datetime,
    gate_results: tuple[GateResult, ...],
    accepted_risks: tuple[str, ...] = (),
    evidence_hashes: dict[str, str] | None = None,
    artifacts: dict[str, str] | None = None,
    test_summary: dict[str, Any] | None = None,
    scenario_metrics: dict[str, Any] | None = None,
    approvals: tuple[dict[str, Any], ...] = (),
    signature: str | None = None,
) -> CertificationResult:
    if generated_at.tzinfo is None:
        raise ValueError("certificate generation time must be timezone-aware")
    evidence_hashes = dict(sorted((evidence_hashes or {}).items()))
    artifacts = dict(sorted((artifacts or evidence_hashes).items()))
    if any(not _is_sha256(value) for value in evidence_hashes.values()):
        raise ValueError("evidence hashes must be lowercase SHA-256 digests")
    if any(not _is_sha256(value) for value in artifacts.values()):
        raise ValueError("artifact hashes must be lowercase SHA-256 digests")
    if any(artifacts.get(reference) != digest for reference, digest in evidence_hashes.items()):
        raise ValueError("artifact hashes must include every evidence hash unchanged")
    test_summary = dict(test_summary or {})
    scenario_metrics = dict(scenario_metrics or {})
    by_name = {gate.name: gate for gate in gate_results}
    declared_gates = tuple(
        by_name.get(name, GateResult(name, False, (), "mandatory evidence missing")) for name in MANDATORY_GATES
    )
    gates = tuple(
        (
            GateResult(gate.name, False, gate.evidence, "evidence content hash is missing")
            if gate.passed and (not gate.evidence or any(item not in evidence_hashes for item in gate.evidence))
            else gate
        )
        for gate in declared_gates
    )
    status = "CERTIFIED" if all(gate.passed for gate in gates) else "NOT_CERTIFIED"
    body = {
        "release_id": release_id,
        "commit": commit,
        "generated_at": generated_at.isoformat(),
        "expires_at": (generated_at + timedelta(days=30)).isoformat(),
        "status": status,
        "gates": [asdict(gate) for gate in gates],
        "risks": accepted_risks,
        "evidence_hashes": evidence_hashes,
        "artifacts": artifacts,
        "test_summary": test_summary,
        "scenario_metrics": scenario_metrics,
        "approvals": approvals,
    }
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return CertificationResult(
        release_id,
        commit,
        generated_at,
        generated_at + timedelta(days=30),
        status,
        gates,
        accepted_risks,
        evidence_hashes,
        digest,
        artifacts,
        test_summary,
        scenario_metrics,
        approvals,
        signature,
    )


def verify_certificate(result: CertificationResult) -> bool:
    body = {
        "release_id": result.release_id,
        "commit": result.commit,
        "generated_at": result.generated_at.isoformat(),
        "expires_at": result.expires_at.isoformat(),
        "status": result.status,
        "gates": [asdict(gate) for gate in result.gates],
        "risks": result.risks,
        "evidence_hashes": result.evidence_hashes,
        "artifacts": result.artifacts,
        "test_summary": result.test_summary,
        "scenario_metrics": result.scenario_metrics,
        "approvals": result.approvals,
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() == result.certificate_hash


def certificate_from_document(document: dict[str, Any]) -> CertificationResult:
    gates = tuple(
        GateResult(
            name=str(item["name"]),
            passed=bool(item["passed"]),
            evidence=tuple(str(reference) for reference in item.get("evidence", [])),
            reason=str(item["reason"]) if item.get("reason") is not None else None,
        )
        for item in document.get("gates", [])
        if isinstance(item, dict)
    )
    result = CertificationResult(
        release_id=str(document["release_id"]),
        commit=str(document["commit"]),
        generated_at=datetime.fromisoformat(str(document["generated_at"])),
        expires_at=datetime.fromisoformat(str(document["expires_at"])),
        status=str(document["status"]),
        gates=gates,
        risks=tuple(str(item) for item in document.get("risks", [])),
        evidence_hashes={str(key): str(value) for key, value in dict(document.get("evidence_hashes", {})).items()},
        certificate_hash=str(document["certificate_hash"]),
        artifacts={str(key): str(value) for key, value in dict(document.get("artifacts", {})).items()},
        test_summary=dict(document.get("test_summary", {})),
        scenario_metrics=dict(document.get("scenario_metrics", {})),
        approvals=tuple(dict(item) for item in document.get("approvals", []) if isinstance(item, dict)),
        signature=str(document["signature"]) if document.get("signature") else None,
    )
    if result.generated_at.tzinfo is None or result.expires_at.tzinfo is None:
        raise ValueError("certificate timestamps must be timezone-aware")
    if result.expires_at != result.generated_at + timedelta(days=30):
        raise ValueError("certificate validity period must be exactly 30 days")
    if tuple(gate.name for gate in result.gates) != MANDATORY_GATES:
        raise ValueError("certificate mandatory gate set is invalid")
    expected_status = "CERTIFIED" if all(gate.passed for gate in result.gates) else "NOT_CERTIFIED"
    if result.status != expected_status:
        raise ValueError("certificate status does not match its mandatory gates")
    if result.status == "CERTIFIED" and not IMMUTABLE_REVISION.fullmatch(result.commit):
        raise ValueError("certified releases require an immutable source revision")
    if any(not _is_sha256(value) for value in result.evidence_hashes.values()):
        raise ValueError("certificate evidence hash is invalid")
    if any(not _is_sha256(value) for value in result.artifacts.values()):
        raise ValueError("certificate artifact hash is invalid")
    if any(result.artifacts.get(reference) != digest for reference, digest in result.evidence_hashes.items()):
        raise ValueError("certificate artifact set does not cover its evidence hashes")
    if any(
        gate.passed
        and (not gate.evidence or any(reference not in result.evidence_hashes for reference in gate.evidence))
        for gate in result.gates
    ):
        raise ValueError("a passing certificate gate has unbound evidence")
    if not verify_certificate(result):
        raise ValueError("certificate content hash is invalid")
    return result


def collect_certificate_metadata(
    evidence_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], tuple[dict[str, Any], ...], tuple[str, ...]]:
    test_summary: dict[str, Any] = {}
    test_path = evidence_root / "test-results.xml"
    if test_path.is_file():
        try:
            root = _safe_xml_root(test_path)
            suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
            test_summary = {
                key: sum(int(suite.attrib.get(key, "0")) for suite in suites)
                for key in ("tests", "failures", "errors", "skipped")
            }
        except (ET.ParseError, OSError, ValueError):
            test_summary = {"status": "INVALID"}
    coverage_path = evidence_root / "coverage.xml"
    if coverage_path.is_file():
        try:
            coverage = _safe_xml_root(coverage_path)
            test_summary["line_coverage"] = float(coverage.attrib.get("line-rate", "0"))
        except (ET.ParseError, OSError, ValueError):
            test_summary["line_coverage"] = 0.0
    scenario_metrics: dict[str, Any] = {}
    for path in (
        evidence_root / "B10" / "batch-run-summary.json",
        evidence_root / "B20" / "e2e-results" / "episode-summary.json",
    ):
        document = _document(path)
        if document:
            scenario_metrics.update(document)
    approvals: list[dict[str, Any]] = []
    attestations = _document(evidence_root / "B20" / "signed-attestations.json")
    for item in (attestations or {}).get("results", []):
        if isinstance(item, dict) and item.get("kind") == "human_acceptance":
            claims = item.get("claims")
            if isinstance(claims, dict):
                approvals.extend(
                    dict(decision) for decision in claims.get("decisions", []) if isinstance(decision, dict)
                )
    risks_document = _document(evidence_root / "B20" / "open-risks.json") or {}
    raw_risks = risks_document.get("risks", risks_document.get("failed_gates", []))
    risks = tuple(str(item) for item in raw_risks) if isinstance(raw_risks, list) else ()
    return test_summary, scenario_metrics, tuple(approvals), risks


def collect_gate_evidence_hashes(evidence_root: Path, gate_results: tuple[GateResult, ...]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for gate in gate_results:
        for reference in gate.evidence:
            relative = Path(reference)
            if relative.parts and relative.parts[0] == "evidence":
                relative = Path(*relative.parts[1:])
            path = evidence_root / relative
            if path.is_file():
                hashes[reference] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(hashes.items()))


def collect_batch_manifests(evidence_root: Path) -> tuple[dict[str, Any], ...]:
    manifests: list[dict[str, Any]] = []
    for batch in range(1, 21):
        path = evidence_root / f"B{batch:02d}" / "manifest.json"
        if not path.exists():
            raise FileNotFoundError(path)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("batch") != f"B{batch:02d}":
            raise ValueError(f"batch manifest mismatch: {path}")
        manifests.append(manifest)
    return tuple(manifests)


def _document(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _verified_document(path: Path) -> dict[str, Any] | None:
    integrity, _ = verify_evidence(path)
    return _document(path) if integrity else None


def _bound_pass(document: dict[str, Any] | None, release_id: str, source_revision: str) -> bool:
    return bool(
        document
        and document.get("status") == "PASS"
        and document.get("release_id") == release_id
        and document.get("source_revision") == source_revision
    )


def _junit_pass(path: Path) -> bool:
    try:
        root = _safe_xml_root(path)
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        return (
            bool(suites)
            and sum(int(suite.attrib.get("tests", "0")) for suite in suites) > 0
            and all(
                int(suite.attrib.get(name, "0")) == 0 for suite in suites for name in ("failures", "errors", "skipped")
            )
        )
    except (OSError, ET.ParseError, ValueError):
        return False


def _scenario_evidence_pass(evidence_root: Path) -> bool:
    summary = _document(evidence_root / "B10" / "batch-run-summary.json")
    episodes = _document(evidence_root / "B20" / "e2e-results" / "episode-summary.json")
    scenarios = (summary or {}).get("scenarios")
    return bool(
        summary
        and summary.get("passed") is True
        and isinstance(scenarios, list)
        and len(scenarios) == 10
        and all(
            isinstance(item, dict)
            and isinstance(item.get("evaluation"), dict)
            and int(item["evaluation"].get("hard_violations", -1)) == 0
            and int(item["evaluation"].get("sla_violations", -1)) == 0
            for item in scenarios
        )
        and episodes
        and int(episodes.get("fault_episodes", 0)) >= 100
        and int(episodes.get("normal_episodes", 0)) >= 50
        and int(episodes.get("hard_violations", -1)) == 0
        and int(episodes.get("sla_violations", -1)) == 0
    )


def evaluate_production_evidence(
    evidence_root: Path, *, release_id: str, source_revision: str
) -> tuple[GateResult, ...]:
    manifests = collect_batch_manifests(evidence_root)
    base_manifests = manifests[:19]

    manifests_bound = len(base_manifests) == 19 and all(
        manifest.get("git_commit") == source_revision for manifest in base_manifests
    )

    def manifest_gate_pass(name: str, required_batches: range) -> bool:
        return manifests_bound and all(
            base_manifests[batch - 1].get("mandatory_gates", {}).get(name) == "PASS" for batch in required_batches
        )

    tests_pass = (
        manifest_gate_pass("tests", range(1, 20))
        and _junit_pass(evidence_root / "test-results.xml")
        and _junit_pass(evidence_root / "postgres-integration.xml")
    )
    schema_catalog = _document(evidence_root / "B02" / "schema-catalog.json")
    contracts_pass = bool(
        manifest_gate_pass("contracts", range(1, 20))
        and schema_catalog
        and schema_catalog.get("source_revision") == source_revision
        and isinstance(schema_catalog.get("openapi"), dict)
        and int(schema_catalog["openapi"].get("path_count", 0)) > 0
    )
    scenarios_pass = manifest_gate_pass("scenario", range(9, 20)) and _scenario_evidence_pass(evidence_root)
    container = _verified_document(evidence_root / "B01" / "container-images.json")
    container_pass = bool(
        container
        and container.get("source_revision") == source_revision
        and container.get("container_build", {}).get("status") == "PASS"
        and container.get("runtime_smoke", {}).get("status") == "PASS"
    )
    immutable_source = IMMUTABLE_REVISION.fullmatch(source_revision) is not None
    source_binding = _verified_document(evidence_root / "B01" / "source-binding.json")
    source_bound = bool(
        source_binding
        and source_binding.get("status") == "PASS"
        and source_binding.get("source_revision") == source_revision
        and source_binding.get("commit") == source_revision
        and source_binding.get("clean") is True
        and isinstance(source_binding.get("tree"), str)
        and GIT_OBJECT_ID.fullmatch(str(source_binding["tree"])) is not None
    )
    request_path = evidence_root / "B20" / "evidence-request.json"
    try:
        request = load_verified_request(request_path)
    except (KeyError, OSError, ValueError):
        request = None
    request_bound = bool(request and request.release_id == release_id and request.source_revision == source_revision)
    request_sha256 = request.request_sha256 if request_bound and request else None
    attestations = _verified_document(evidence_root / "B20" / "signed-attestations.json")
    verified_attestations: dict[str, Any] | None = None
    material = (attestations or {}).get("verification_material")
    if request and isinstance(material, dict):
        try:
            verified_attestations = verify_verification_material(
                material,
                evidence_root=evidence_root,
                release_id=release_id,
                source_revision=source_revision,
                release_requester=request.requested_by,
                request_sha256=request.request_sha256,
                bound_input_artifacts=request.input_artifacts,
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            verified_attestations = None
    attestation_results: dict[str, str] = {}
    for item in (verified_attestations or {}).get("results", []):
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "FAIL"))
        if status == "PASS":
            try:
                expires_at = datetime.fromisoformat(str(item["expires_at"]))
                if expires_at.tzinfo is None or expires_at <= datetime.now(UTC):
                    status = "FAIL"
            except (KeyError, ValueError):
                status = "FAIL"
        attestation_results[str(item.get("kind"))] = status
    attestations_bound = bool(
        request_bound
        and _bound_pass(attestations, release_id, source_revision)
        and attestations
        and attestations.get("request_sha256") == request_sha256
        and attestations.get("cryptographic_verification") is True
        and verified_attestations
        and verified_attestations.get("status") == "PASS"
        and verified_attestations.get("trust_policy_sha256") == attestations.get("trust_policy_sha256")
        and _is_sha256(attestations.get("trust_policy_sha256"))
    )
    performance = _verified_document(evidence_root / "B20" / "production-performance-report.json")
    restore = _verified_document(evidence_root / "B20" / "restore-rehearsal.json")
    external = _verified_document(evidence_root / "B20" / "external-integrations.json")
    performance_pass = bool(
        request_bound
        and _bound_pass(performance, release_id, source_revision)
        and performance
        and performance.get("request_sha256") == request_sha256
        and performance.get("production_evidence") is True
    )
    restore_pass = bool(
        request_bound
        and _bound_pass(restore, release_id, source_revision)
        and restore
        and restore.get("request_sha256") == request_sha256
    )
    external_pass = bool(
        request_bound
        and _bound_pass(external, release_id, source_revision)
        and external
        and external.get("request_sha256") == request_sha256
    )
    security_pass = attestations_bound and attestation_results.get("penetration_test") == "PASS"
    acceptance_pass = attestations_bound and attestation_results.get("human_acceptance") == "PASS"
    return (
        GateResult(
            "build",
            tests_pass and container_pass and immutable_source and source_bound,
            (
                "evidence/B01/build.log",
                "evidence/B01/container-images.json",
                "evidence/B01/source-binding.json",
            ),
            (
                None
                if container_pass and immutable_source and source_bound
                else "clean immutable source binding or current hardened container build/runtime evidence is missing"
            ),
        ),
        GateResult(
            "tests",
            tests_pass,
            ("evidence/test-results.xml", "evidence/postgres-integration.xml", "evidence/coverage.xml"),
        ),
        GateResult("contracts", contracts_pass, ("evidence/B02/schema-catalog.json",)),
        GateResult(
            "security",
            security_pass,
            ("evidence/B20/signed-attestations.json",),
            None if security_pass else "release-bound independent penetration attestation is missing",
        ),
        GateResult(
            "scenarios",
            scenarios_pass,
            (
                "evidence/B10/batch-run-summary.json",
                "evidence/B20/e2e-results/episode-summary.json",
            ),
        ),
        GateResult(
            "performance",
            performance_pass,
            ("evidence/B20/production-performance-report.json",),
            None if performance_pass else "release-bound production load report is missing",
        ),
        GateResult(
            "backup_restore",
            restore_pass,
            ("evidence/B20/restore-rehearsal.json",),
            None if restore_pass else "release-bound database and object restore rehearsal is missing",
        ),
        GateResult(
            "external_integrations",
            external_pass,
            ("evidence/B20/external-integrations.json",),
            None if external_pass else "release-bound OIDC, Kubernetes, Slurm, meter and EMS acceptance is missing",
        ),
        GateResult(
            "acceptance",
            acceptance_pass,
            ("evidence/B20/signed-attestations.json",),
            None if acceptance_pass else "release-bound human acceptance is missing",
        ),
    )
