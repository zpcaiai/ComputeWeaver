from __future__ import annotations

from .contracts import ScheduleInput, SchedulePlan
from .strategies import schedule_priority_edf


def schedule(request: ScheduleInput) -> SchedulePlan:
    return schedule_priority_edf(request)
