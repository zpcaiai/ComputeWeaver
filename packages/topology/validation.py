from __future__ import annotations

from collections import defaultdict, deque
from decimal import Decimal

from .models import Asset, Relationship


def validate_graph(assets: tuple[Asset, ...], relationships: tuple[Relationship, ...]) -> None:
    by_id = {asset.id: asset for asset in assets}
    if len(by_id) != len(assets):
        raise ValueError("duplicate asset ID")
    children: dict[str, list[str]] = defaultdict(list)
    indegree = {asset.id: 0 for asset in assets}
    for relationship in relationships:
        if relationship.parent_id not in by_id or relationship.child_id not in by_id:
            raise ValueError("orphan topology relationship")
        if by_id[relationship.parent_id].tenant_id != by_id[relationship.child_id].tenant_id:
            raise ValueError("cross-tenant topology relationship")
        children[relationship.parent_id].append(relationship.child_id)
        indegree[relationship.child_id] += 1
    queue = deque(item for item, count in indegree.items() if count == 0)
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for child in children[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(assets):
        raise ValueError("topology contains a cycle")

    for parent_id, child_ids in children.items():
        parent_capacity = by_id[parent_id].capacity_kw
        child_capacity = sum((by_id[item].capacity_kw for item in child_ids), Decimal(0))
        if parent_capacity > 0 and child_capacity > parent_capacity:
            raise ValueError(f"child capacity exceeds boundary {parent_id}")


def descendants(assets: tuple[Asset, ...], relationships: tuple[Relationship, ...], root: str) -> tuple[Asset, ...]:
    by_id = {asset.id: asset for asset in assets}
    if root not in by_id:
        raise KeyError(root)
    edges: dict[str, list[str]] = defaultdict(list)
    for relationship in relationships:
        edges[relationship.parent_id].append(relationship.child_id)
    output: list[Asset] = []
    queue = deque(edges[root])
    seen: set[str] = set()
    while queue:
        item = queue.popleft()
        if item in seen:
            continue
        seen.add(item)
        output.append(by_id[item])
        queue.extend(edges[item])
    return tuple(output)
