from __future__ import annotations

from dataclasses import dataclass

from packages.scheduling.contracts import TimeSlot


@dataclass(frozen=True, slots=True)
class HorizonPartition:
    locked: tuple[TimeSlot, ...]
    controllable: tuple[TimeSlot, ...]
    forecast_only: tuple[TimeSlot, ...]


def partition_horizon(
    slots: tuple[TimeSlot, ...],
    *,
    locked_count: int,
    controllable_count: int,
) -> HorizonPartition:
    if locked_count < 0 or controllable_count < 1:
        raise ValueError("invalid MPC horizon sizes")
    ordered = tuple(sorted(slots, key=lambda item: (item.starts_at, item.index)))
    if len({item.index for item in ordered}) != len(ordered):
        raise ValueError("MPC horizon contains duplicate slots")
    locked = ordered[:locked_count]
    controllable = ordered[locked_count : locked_count + controllable_count]
    if not controllable:
        raise ValueError("MPC horizon has no controllable interval")
    return HorizonPartition(locked, controllable, ordered[locked_count + controllable_count :])
