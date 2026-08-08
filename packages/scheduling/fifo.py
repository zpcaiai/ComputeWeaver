from __future__ import annotations

from .contracts import ScheduleInput, SchedulePlan
from .strategies import schedule_fifo


def schedule(request: ScheduleInput) -> SchedulePlan:
    return schedule_fifo(request)
