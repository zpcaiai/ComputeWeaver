from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from packages.certification.evidence import write_evidence
from packages.certification.requests import load_verified_request
from packages.dr.rehearsal import (
    enforce_recovery_objectives,
    run_restore_rehearsal,
    validate_restore_contract,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore PostgreSQL and objects into isolated rehearsal targets")
    parser.add_argument("configuration", type=Path)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("evidence/B20/restore-rehearsal.json"))
    arguments = parser.parse_args()
    document = json.loads(arguments.configuration.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit("restore configuration must be a JSON object")
    request = load_verified_request(arguments.request)
    for field, expected in (
        ("release_id", request.release_id),
        ("source_revision", request.source_revision),
        ("request_sha256", request.request_sha256),
    ):
        configured = document.get(field)
        if configured is not None and str(configured) != expected:
            raise SystemExit(f"restore configuration {field} does not match the evidence request")
        document[field] = expected
    validate_restore_contract(
        document,
        postgres_contract=dict(request.requirements["postgres_restore"]),
        object_contract=dict(request.requirements["object_restore"]),
    )
    result = enforce_recovery_objectives(
        run_restore_rehearsal(document),
        postgres_contract=dict(request.requirements["postgres_restore"]),
        object_contract=dict(request.requirements["object_restore"]),
    )
    write_evidence(
        arguments.output,
        result,
        command=shlex.join(["python", "-m", "scripts.run_restore_rehearsal", *sys.argv[1:]]),
        suite_name="restore-rehearsal",
    )
    print(json.dumps(result, indent=2, default=str, sort_keys=True))
    raise SystemExit(0 if result["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()
