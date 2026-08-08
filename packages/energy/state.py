from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class StateKind(StrEnum):
    ACTUAL = "actual"
    FORECAST = "forecast"
    PLANNED = "planned"


@dataclass(frozen=True, slots=True)
class EnergyState:
    site_id: str
    observed_at: datetime
    kind: StateKind
    grid_kw: Decimal
    pv_kw: Decimal
    battery_soc: Decimal
    facility_kw: Decimal
    source_version: str
