from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from packages.certification.evidence import write_evidence
from packages.certification.external import load_manifest, run_external_acceptance
from packages.certification.requests import load_verified_request


def main() -> None:
    parser = argparse.ArgumentParser(description="Run fail-closed acceptance against real external integrations")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("evidence/B20/external-integrations.json"))
    arguments = parser.parse_args()
    request = load_verified_request(arguments.request)
    manifest = load_manifest(arguments.manifest)
    for field, expected in (
        ("release_id", request.release_id),
        ("source_revision", request.source_revision),
        ("request_sha256", request.request_sha256),
    ):
        configured = manifest.get(field)
        if configured is not None and str(configured) != expected:
            raise SystemExit(f"acceptance manifest {field} does not match the evidence request")
        manifest[field] = expected
    result = run_external_acceptance(manifest)
    write_evidence(
        arguments.output,
        result,
        command=shlex.join(["python", "-m", "scripts.run_external_acceptance", *sys.argv[1:]]),
        suite_name="external-integration-acceptance",
    )
    print(json.dumps(result, indent=2, default=str, sort_keys=True))
    raise SystemExit(0 if result["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()
