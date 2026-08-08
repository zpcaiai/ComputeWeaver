from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from packages.certification.attestations import (
    build_verification_material,
    issue_attestation_from_config,
    load_bundle,
    verify_attestation_bundle,
    write_attestation,
)
from packages.certification.evidence import write_evidence
from packages.certification.load import (
    LoadThresholds,
    report_dict,
    run_load_gate,
    validate_production_contract,
)
from packages.certification.requests import create_request_from_config, load_verified_request, write_request
from packages.secrets import CredentialResolver


def _load_object(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"configuration must be a JSON object: {path}")
    return document


def _load_client(arguments: argparse.Namespace) -> httpx.Client:
    resolver = CredentialResolver.from_env()
    headers = {"Accept": "application/json"}
    if arguments.token_ref:
        headers["Authorization"] = f"Bearer {resolver.resolve(arguments.token_ref)}"
    if bool(arguments.client_certificate_ref) != bool(arguments.client_key_ref):
        raise ValueError("load client certificate and key references must be configured together")
    certificate: tuple[str, str] | None = None
    if arguments.client_certificate_ref and arguments.client_key_ref:
        certificate = (
            str(resolver.resolve_file(arguments.client_certificate_ref)),
            str(resolver.resolve_file(arguments.client_key_ref)),
        )
    verify: str | bool = True
    if arguments.ca_bundle_ref:
        verify = str(resolver.resolve_file(arguments.ca_bundle_ref))
    return httpx.Client(headers=headers, verify=verify, cert=certificate, follow_redirects=False)


def _client_requester(client: httpx.Client, target: str, timeout_seconds: float) -> tuple[bool, float]:
    started = time.perf_counter()
    try:
        response = client.get(target, timeout=timeout_seconds)
        success = 200 <= response.status_code < 400
    except httpx.HTTPError:
        success = False
    return success, (time.perf_counter() - started) * 1000


def main() -> None:
    parser = argparse.ArgumentParser(description="Run signed attestations or an explicit production load gate")
    subcommands = parser.add_subparsers(dest="command", required=True)
    attest = subcommands.add_parser("attestations")
    attest.add_argument("bundle", type=Path)
    attest.add_argument("--request", type=Path, required=True)
    attest.add_argument("--policy", type=Path, required=True)
    attest.add_argument("--output", type=Path, default=Path("evidence/B20/signed-attestations.json"))
    request_parser = subcommands.add_parser("request")
    request_parser.add_argument("configuration", type=Path)
    request_parser.add_argument("--output", type=Path, default=Path("evidence/B20/evidence-request.json"))
    sign = subcommands.add_parser("sign")
    sign.add_argument("configuration", type=Path)
    sign.add_argument("--output", type=Path, required=True)
    load = subcommands.add_parser("load")
    load.add_argument("target")
    load.add_argument("--requests", type=int, required=True)
    load.add_argument("--concurrency", type=int, required=True)
    load.add_argument("--p95-ms", type=float, required=True)
    load.add_argument("--p99-ms", type=float, required=True)
    load.add_argument("--max-error-rate", type=float, required=True)
    load.add_argument("--release-id", required=True)
    load.add_argument("--source-revision", required=True)
    load.add_argument("--request", type=Path, required=True)
    load.add_argument("--token-ref")
    load.add_argument("--ca-bundle-ref")
    load.add_argument("--client-certificate-ref")
    load.add_argument("--client-key-ref")
    load.add_argument("--output", type=Path, default=Path("evidence/B20/production-performance-report.json"))
    arguments = parser.parse_args()
    command = shlex.join(["python", "-m", "scripts.run_production_gates", *sys.argv[1:]])
    if arguments.command == "request":
        request = create_request_from_config(_load_object(arguments.configuration))
        digest = write_request(arguments.output, request, command=command)
        print(
            json.dumps(
                {
                    "status": "PENDING_EXTERNAL_EVIDENCE",
                    "request_sha256": request.request_sha256,
                    "file_sha256": digest,
                },
                indent=2,
            )
        )
        return
    if arguments.command == "sign":
        token = issue_attestation_from_config(_load_object(arguments.configuration))
        digest = write_attestation(arguments.output, token)
        print(json.dumps({"status": "SIGNED", "token_sha256": digest, "output": str(arguments.output)}, indent=2))
        return
    request = load_verified_request(arguments.request)
    if arguments.command == "attestations":
        bundle = load_bundle(arguments.bundle)
        if bundle.get("release_id") != request.release_id or bundle.get("source_revision") != request.source_revision:
            raise SystemExit("attestation bundle release does not match the evidence request")
        policy_digest = hashlib.sha256(arguments.policy.read_bytes()).hexdigest()
        if request.input_artifacts.get(arguments.policy.name) != policy_digest:
            raise SystemExit("attestation trust policy is not bound to the evidence request")
        verification_material = build_verification_material(
            bundle,
            trust_policy_path=arguments.policy,
            evidence_root=arguments.output.parent.parent,
        )
        for key_name, key_material in verification_material["public_keys"].items():
            if request.input_artifacts.get(key_name) != key_material["sha256"]:
                raise SystemExit(f"attestation public key is not bound to the evidence request: {key_name}")
        result = verify_attestation_bundle(
            bundle,
            expected_request_sha256=request.request_sha256,
            trust_policy=_load_object(arguments.policy),
        )
        result["trust_policy_sha256"] = policy_digest
        result["verification_material"] = verification_material
        suite_name = "signed-production-attestations"
    else:
        if arguments.release_id != request.release_id or arguments.source_revision != request.source_revision:
            raise SystemExit("load gate release does not match the evidence request")
        thresholds = LoadThresholds(arguments.p95_ms, arguments.p99_ms, arguments.max_error_rate)
        validate_production_contract(
            target=arguments.target,
            requests=arguments.requests,
            concurrency=arguments.concurrency,
            thresholds=thresholds,
            contract=dict(request.requirements["production_load"]),
        )
        with _load_client(arguments) as client:
            result = report_dict(
                run_load_gate(
                    target=arguments.target,
                    requests=arguments.requests,
                    concurrency=arguments.concurrency,
                    thresholds=thresholds,
                    requester=lambda target, timeout: _client_requester(client, target, timeout),
                    release_id=arguments.release_id,
                    source_revision=arguments.source_revision,
                    request_sha256=request.request_sha256,
                )
            )
        suite_name = "production-load"
    write_evidence(arguments.output, result, command=command, suite_name=suite_name)
    print(json.dumps(result, indent=2, default=str, sort_keys=True))
    raise SystemExit(0 if result["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()
