from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from .compiler import compile_scenario, run_scenario


def main() -> None:
    parser = argparse.ArgumentParser(prog="scenario")
    parser.add_argument("command", choices=("validate", "run", "batch"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    document = yaml.safe_load(args.path.read_text(encoding="utf-8"))
    if args.command == "batch":
        if not isinstance(document, list):
            raise SystemExit("batch scenario file must contain a list")
        results = []
        for item in document:
            scenario = compile_scenario(item)
            events, evaluation = run_scenario(scenario)
            results.append({"name": scenario.name, "events": len(events), "evaluation": evaluation.as_dict()})
        print(json.dumps({"scenarios": results}, indent=2))
        return
    scenario = compile_scenario(document)
    if args.command == "validate":
        print(json.dumps({"valid": True, "name": scenario.name}))
        return
    events, evaluation = run_scenario(scenario)
    print(json.dumps({"events": len(events), "evaluation": evaluation.as_dict()}, indent=2))


if __name__ == "__main__":
    main()
