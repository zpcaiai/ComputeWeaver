from __future__ import annotations

from .contracts import ScheduleInput, SchedulePlan
from .strategies import schedule_price_aware


def schedule(request: ScheduleInput) -> SchedulePlan:
    return schedule_price_aware(request)
