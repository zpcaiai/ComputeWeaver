from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .metrics import evaluate_events


def _events(path: Path) -> list[dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    events = document.get("events") if isinstance(document, dict) else document
    if not isinstance(events, list) or any(not isinstance(item, dict) for item in events):
        raise SystemExit(f"invalid event list: {path}")
    return events


def main() -> None:
    parser = argparse.ArgumentParser(prog="evaluate")
    parser.add_argument("command", choices=("compare",))
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    baseline = evaluate_events(_events(args.baseline))
    candidate = evaluate_events(_events(args.candidate))
    baseline_data = baseline.as_dict()
    candidate_data = candidate.as_dict()
    deltas: dict[str, str] = {}
    for key in baseline_data.keys() & candidate_data.keys():
        try:
            deltas[key] = str(Decimal(str(candidate_data[key])) - Decimal(str(baseline_data[key])))
        except InvalidOperation:
            continue
    print(json.dumps({"baseline": baseline_data, "candidate": candidate_data, "deltas": deltas}, indent=2))


if __name__ == "__main__":
    main()
