from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from packages.evaluator.metrics import Evaluation, evaluate_events


@dataclass(frozen=True, slots=True)
class ReplayResult:
    event_count: int
    event_hash: str
    evaluation: Evaluation


def replay(events: list[dict[str, Any]]) -> ReplayResult:
    ordered = sorted(events, key=lambda item: (item["timestamp"], item["sequence"]))
    digest = hashlib.sha256(json.dumps(ordered, sort_keys=True).encode()).hexdigest()
    return ReplayResult(len(ordered), digest, evaluate_events(ordered))
