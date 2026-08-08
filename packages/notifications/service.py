from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class Route:
    event_type: str
    channel: str
    destination: str
    minimum_severity: int
    suppress_for: timedelta
    escalation_after: timedelta | None = None


class NotificationRouter:
    def __init__(self) -> None:
        self._routes: list[Route] = []
        self._sent: dict[tuple[str, str], datetime] = {}

    def add_route(self, route: Route) -> None:
        self._routes.append(route)

    def route(self, *, event_id: str, event_type: str, severity: int, now: datetime) -> tuple[dict[str, str], ...]:
        messages: list[dict[str, str]] = []
        for route in self._routes:
            if route.event_type != event_type or severity < route.minimum_severity:
                continue
            key = (event_type, route.destination)
            last = self._sent.get(key)
            if last and now - last < route.suppress_for:
                continue
            self._sent[key] = now
            messages.append(
                {
                    "event_id": event_id,
                    "channel": route.channel,
                    "destination": route.destination,
                    "status": "routed",
                }
            )
        return tuple(messages)
