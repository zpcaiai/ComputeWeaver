from __future__ import annotations

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from packages.certification.evidence import write_evidence
from packages.certification.source import inspect_git_source

ROOT = Path(__file__).resolve().parents[1]
MAX_XML_BYTES = 20 * 1024 * 1024


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _junit_summary(path: Path) -> dict[str, int]:
    content = path.read_bytes()
    if len(content) > MAX_XML_BYTES:
        raise ValueError("JUnit result exceeds the 20 MiB limit")
    upper = content.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("JUnit result cannot contain DTD or entity declarations")
    root = ET.fromstring(content)  # noqa: S314 - bounded input with DTD/entity rejection
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if not suites:
        raise ValueError("JUnit result does not contain a test suite")
    return {
        key: sum(int(suite.attrib.get(key, "0")) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }


def record_test_run(junit: Path, output: Path, *, coverage: Path | None, suite_name: str) -> dict[str, object]:
    binding = inspect_git_source(ROOT)
    summary = _junit_summary(junit)
    passed = (
        binding.status == "PASS"
        and summary["tests"] > 0
        and summary["failures"] == 0
        and summary["errors"] == 0
        and summary["skipped"] == 0
        and (coverage is None or coverage.is_file())
    )
    document: dict[str, object] = {
        "status": "PASS" if passed else "FAIL",
        "source_revision": binding.commit,
        "tree": binding.tree,
        "clean": binding.clean,
        "junit": str(junit),
        "junit_sha256": _sha256(junit),
        "test_summary": summary,
    }
    if coverage is not None and coverage.is_file():
        document["coverage"] = str(coverage)
        document["coverage_sha256"] = _sha256(coverage)
    write_evidence(output, document, command=f"record test run: {suite_name}", suite_name=suite_name)
    return document


def main() -> None:
    parser = argparse.ArgumentParser(description="Bind passing test artifacts to a clean immutable Git revision")
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--coverage", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--suite-name", required=True)
    arguments = parser.parse_args()
    try:
        document = record_test_run(
            arguments.junit,
            arguments.output,
            coverage=arguments.coverage,
            suite_name=arguments.suite_name,
        )
    except (OSError, ValueError, ET.ParseError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(document, indent=2, sort_keys=True))
    raise SystemExit(0 if document["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()
