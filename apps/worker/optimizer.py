from __future__ import annotations

from dataclasses import asdict
from typing import Any

from packages.jobs.queue import DurableJob
from packages.optimization.solvers import HighsSolver
from packages.scheduling.serialization import parse_schedule_input


async def optimization_handler(job: DurableJob) -> dict[str, Any]:
    request = parse_schedule_input(dict(job.payload["schedule"]), job.tenant_id)
    result = HighsSolver().solve(request, timeout_seconds=float(job.payload.get("timeout_seconds", 10)))
    if result.plan is None:
        raise RuntimeError(f"optimization did not produce a safe plan: {result.status} {result.diagnostics}")
    return asdict(result)
