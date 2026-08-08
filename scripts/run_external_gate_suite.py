from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from packages.certification.evidence import write_evidence
from packages.certification.suite import run_external_gate_suite

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run all request-bound external production gates")
    parser.add_argument("configuration", type=Path)
    parser.add_argument("--output", type=Path, default=Path("evidence/B20/external-gate-suite.json"))
    arguments = parser.parse_args()
    document = json.loads(arguments.configuration.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit("external gate suite configuration must be a JSON object")
    report = run_external_gate_suite(ROOT, document, configuration_base=arguments.configuration.parent)
    rendered = report.as_document()
    write_evidence(
        arguments.output,
        rendered,
        command=shlex.join(["python", "-m", "scripts.run_external_gate_suite", *sys.argv[1:]]),
        suite_name="external-production-gate-suite",
    )
    print(json.dumps(rendered, indent=2, sort_keys=True))
    raise SystemExit(0 if report.status == "PASS" else 2)


if __name__ == "__main__":
    main()
