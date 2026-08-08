from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from packages.certification.evidence import write_evidence
from packages.certification.preflight import run_preflight

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Fail-closed production release prerequisite checks")
    parser.add_argument("configuration", type=Path)
    parser.add_argument("--output", type=Path, default=Path("evidence/B20/production-preflight.json"))
    arguments = parser.parse_args()
    document = json.loads(arguments.configuration.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit("preflight configuration must be a JSON object")
    report = run_preflight(ROOT, document)
    rendered = report.as_document()
    write_evidence(
        arguments.output,
        rendered,
        command=shlex.join(["python", "-m", "scripts.run_production_preflight", *sys.argv[1:]]),
        suite_name="production-preflight",
    )
    print(json.dumps(rendered, indent=2, sort_keys=True))
    raise SystemExit(0 if report.status == "PASS" else 2)


if __name__ == "__main__":
    main()
