from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal

from .models import Asset, AssetType


@dataclass(frozen=True, slots=True)
class ImportResult:
    assets: tuple[Asset, ...]
    conflicts: tuple[str, ...]


def import_csv(content: str, *, tenant_id: str, dry_run: bool = True) -> ImportResult:
    del dry_run  # parsing is side-effect free in both modes; registry publication is separate.
    assets: list[Asset] = []
    conflicts: list[str] = []
    seen: set[str] = set()
    for row_number, row in enumerate(csv.DictReader(io.StringIO(content)), start=2):
        try:
            if row["id"] in seen:
                raise ValueError("duplicate id")
            seen.add(row["id"])
            assets.append(
                Asset(
                    id=row["id"],
                    tenant_id=tenant_id,
                    site_id=row["site_id"],
                    kind=AssetType(row["kind"]),
                    name=row["name"],
                    capacity_kw=Decimal(row.get("capacity_kw") or 0),
                )
            )
        except (KeyError, ValueError) as error:
            conflicts.append(f"row {row_number}: {error}")
    return ImportResult(tuple(assets), tuple(conflicts))
