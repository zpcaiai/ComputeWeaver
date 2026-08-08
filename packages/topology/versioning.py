from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import TopologySnapshot


@dataclass(frozen=True, slots=True)
class AssetChange:
    asset_id: str
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TopologyDiff:
    from_version: int
    to_version: int
    added_assets: frozenset[str]
    removed_assets: frozenset[str]
    changed_assets: tuple[AssetChange, ...]
    added_relationships: int
    removed_relationships: int
    capacity_delta_kw: Decimal


def compare_versions(left: TopologySnapshot, right: TopologySnapshot) -> TopologyDiff:
    if left.tenant_id != right.tenant_id:
        raise PermissionError("cannot compare topology versions across tenants")
    left_assets = {asset.id: asset for asset in left.assets}
    right_assets = {asset.id: asset for asset in right.assets}
    changed: list[AssetChange] = []
    for asset_id in sorted(left_assets.keys() & right_assets.keys()):
        before = left_assets[asset_id]
        after = right_assets[asset_id]
        fields = tuple(
            name
            for name in (
                "site_id",
                "kind",
                "name",
                "capacity_kw",
                "attributes",
                "effective",
                "decommissioned_at",
            )
            if getattr(before, name) != getattr(after, name)
        )
        if fields:
            changed.append(AssetChange(asset_id, fields))
    left_relationships = set(left.relationships)
    right_relationships = set(right.relationships)
    left_capacity = sum((asset.capacity_kw for asset in left.assets), Decimal(0))
    right_capacity = sum((asset.capacity_kw for asset in right.assets), Decimal(0))
    return TopologyDiff(
        left.version,
        right.version,
        frozenset(right_assets.keys() - left_assets.keys()),
        frozenset(left_assets.keys() - right_assets.keys()),
        tuple(changed),
        len(right_relationships - left_relationships),
        len(left_relationships - right_relationships),
        right_capacity - left_capacity,
    )
