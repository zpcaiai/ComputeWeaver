from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(slots=True)
class VirtualClock:
    now: datetime
    step_size: timedelta
    speed: float = 1.0
    running: bool = False

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            raise ValueError("virtual clock requires a timezone-aware start")
        if self.step_size <= timedelta(0):
            raise ValueError("virtual clock step must be positive")
        if self.speed <= 0:
            raise ValueError("virtual clock speed must be positive")

    def start(self) -> None:
        self.running = True

    def pause(self) -> None:
        self.running = False

    def step(self, count: int = 1) -> datetime:
        if count < 1:
            raise ValueError("virtual clock step count must be positive")
        self.now += self.step_size * count
        return self.now
