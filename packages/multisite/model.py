from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Site:
    id: str
    region: str
    available_gpus: int
    grid_limit_kw: Decimal
    energy_price: Decimal
    carbon_intensity: Decimal
    online: bool = True


@dataclass(frozen=True, slots=True)
class SiteLink:
    source: str
    destination: str
    bandwidth_gbps: Decimal
    latency_ms: Decimal
    transfer_cost_per_gb: Decimal
    online: bool = True
