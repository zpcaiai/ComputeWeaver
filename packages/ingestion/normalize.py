from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from .raw import RawEvent

POWER_FACTORS = {"W": Decimal("0.001"), "kW": Decimal(1), "MW": Decimal(1000)}
ENERGY_FACTORS = {"Wh": Decimal("0.001"), "kWh": Decimal(1), "MWh": Decimal(1000)}


@dataclass(frozen=True, slots=True)
class NormalizedPoint:
    id: str
    tenant_id: str
    source: str
    metric: str
    timestamp: datetime
    value: Decimal
    unit: str
    raw_event_id: str
    raw_payload_hash: str
    transformation: str


class Normalizer:
    def normalize(self, event: RawEvent) -> NormalizedPoint:
        payload = event.payload
        try:
            metric = str(payload["metric"])
            timestamp = datetime.fromisoformat(str(payload["timestamp"]))
            value = Decimal(str(payload["value"]))
            unit = str(payload["unit"])
        except (KeyError, ValueError) as error:
            raise ValueError("invalid raw signal payload") from error
        if timestamp.tzinfo is None:
            raise ValueError("signal timestamp must be timezone-aware")
        if unit in POWER_FACTORS:
            value *= POWER_FACTORS[unit]
            normalized_unit = "kW"
        elif unit in ENERGY_FACTORS:
            value *= ENERGY_FACTORS[unit]
            normalized_unit = "kWh"
        else:
            normalized_unit = unit
        return NormalizedPoint(
            id=f"norm-{event.id}",
            tenant_id=event.tenant_id,
            source=event.source,
            metric=metric,
            timestamp=timestamp.astimezone(UTC),
            value=value,
            unit=normalized_unit,
            raw_event_id=event.id,
            raw_payload_hash=event.payload_hash,
            transformation=f"{unit}->{normalized_unit}",
        )
