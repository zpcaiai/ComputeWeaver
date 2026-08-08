from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from .api import ErrorEnvelope
from .events import EventEnvelope

MODELS: dict[str, type[BaseModel]] = {"error-envelope": ErrorEnvelope, "event-envelope": EventEnvelope}


def export_schemas(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name, model in MODELS.items():
        (destination / f"{name}.json").write_text(
            json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def compatible(old: dict[str, object], new: dict[str, object]) -> tuple[bool, list[str]]:
    old_required = set(cast(list[str], old.get("required", [])))
    new_required = set(cast(list[str], new.get("required", [])))
    old_properties = set(cast(dict[str, Any], old.get("properties", {})))
    new_properties = set(cast(dict[str, Any], new.get("properties", {})))
    issues = [f"removed property: {name}" for name in sorted(old_properties - new_properties)]
    issues += [f"new required property: {name}" for name in sorted(new_required - old_required)]
    return not issues, issues


def main() -> None:
    parser = argparse.ArgumentParser(prog="schema")
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("destination", type=Path, nargs="?", default=Path("schemas/json"))
    compare = sub.add_parser("compatibility")
    compare.add_argument("old", type=Path)
    compare.add_argument("new", type=Path)
    diff = sub.add_parser("diff")
    diff.add_argument("old", type=Path)
    diff.add_argument("new", type=Path)
    args = parser.parse_args()
    if args.command == "export":
        export_schemas(args.destination)
        return
    old = json.loads(args.old.read_text(encoding="utf-8"))
    new = json.loads(args.new.read_text(encoding="utf-8"))
    ok, issues = compatible(old, new)
    print(json.dumps({"compatible": ok, "issues": issues}, indent=2))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
