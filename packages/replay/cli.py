from __future__ import annotations

import argparse
import json
from pathlib import Path

from .service import replay


def main() -> None:
    parser = argparse.ArgumentParser(prog="replay")
    parser.add_argument("command", choices=("run",))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    document = json.loads(args.path.read_text(encoding="utf-8"))
    events = document.get("events") if isinstance(document, dict) else document
    if not isinstance(events, list):
        raise SystemExit("replay input must be an event list")
    result = replay(events)
    print(
        json.dumps(
            {
                "event_count": result.event_count,
                "event_hash": result.event_hash,
                "evaluation": result.evaluation.as_dict(),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
