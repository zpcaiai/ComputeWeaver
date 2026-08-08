from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from packages.domain.time import TimeInterval


class AssetType(StrEnum):
    ORGANIZATION = "organization"
    REGION = "region"
    CAMPUS = "campus"
    SITE = "site"
    DATA_CENTER = "data_center"
    ROOM = "room"
    RACK = "rack"
    POWER_ZONE = "power_zone"
    COOLING_ZONE = "cooling_zone"
    COMPUTE_NODE = "compute_node"
    ENERGY_ASSET = "energy_asset"


@dataclass(frozen=True, slots=True)
class Asset:
    id: str
    tenant_id: str
    site_id: str
    kind: AssetType
    name: str
    capacity_kw: Decimal = Decimal(0)
    attributes: dict[str, str] = field(default_factory=dict)
    effective: TimeInterval | None = None
    decommissioned_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Relationship:
    parent_id: str
    child_id: str
    kind: str = "contains"


@dataclass(frozen=True, slots=True)
class TopologySnapshot:
    version: int
    tenant_id: str
    assets: tuple[Asset, ...]
    relationships: tuple[Relationship, ...]
    published_at: datetime
    etag: str

    def asset(self, asset_id: str) -> Asset:
        for asset in self.assets:
            if asset.id == asset_id:
                return asset
        raise KeyError(asset_id)

    def active_at(self, moment: datetime) -> tuple[Asset, ...]:
        return tuple(
            asset
            for asset in self.assets
            if (asset.effective is None or asset.effective.contains(moment))
            and (asset.decommissioned_at is None or moment < asset.decommissioned_at)
        )
