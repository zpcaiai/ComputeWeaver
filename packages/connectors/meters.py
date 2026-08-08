from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import httpx

from packages.ingestion.raw import RawEvent


class HttpsMeterConnector:
    """HTTPS interval-meter connector with cursor-based incremental ingestion."""

    def __init__(
        self,
        *,
        connector_id: str,
        base_url: str,
        token: str | None = None,
        ca_bundle: str | bool = True,
        client_certificate: tuple[str, str] | None = None,
        timeout_seconds: float = 15,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("meter connector URL must use HTTPS")
        self.connector_id = connector_id
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.ca_bundle = ca_bundle
        self.client_certificate = client_certificate
        self.timeout_seconds = timeout_seconds

    def pull(
        self,
        *,
        tenant_id: str,
        start: datetime,
        end: datetime,
        cursor: str | None = None,
    ) -> tuple[tuple[RawEvent, ...], str | None]:
        if end <= start:
            raise ValueError("meter interval end must follow start")
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        params = {"start": start.isoformat(), "end": end.isoformat()}
        if cursor:
            params["cursor"] = cursor
        with httpx.Client(
            base_url=self.base_url,
            headers=headers,
            verify=self.ca_bundle,
            cert=self.client_certificate,
            timeout=self.timeout_seconds,
            follow_redirects=False,
        ) as client:
            response = client.get("/v1/intervals", params=params)
            response.raise_for_status()
            document = response.json()
        intervals = document.get("intervals") if isinstance(document, dict) else None
        next_cursor = document.get("next_cursor") if isinstance(document, dict) else None
        if not isinstance(intervals, list) or (next_cursor is not None and not isinstance(next_cursor, str)):
            raise ConnectionError("meter response is malformed")
        received_at = datetime.now(UTC)
        events: list[RawEvent] = []
        for raw in intervals:
            if not isinstance(raw, dict):
                raise ConnectionError("meter interval must be an object")
            payload: dict[str, Any] = {
                "metric": str(raw["metric"]),
                "timestamp": str(raw["timestamp"]),
                "value": str(raw["value"]),
                "unit": str(raw["unit"]),
                "meter_id": str(raw["meter_id"]),
            }
            source_id = raw.get("id")
            if not source_id:
                canonical = json.dumps(payload, sort_keys=True).encode()
                source_id = hashlib.sha256(canonical).hexdigest()[:24]
            events.append(
                RawEvent.create(
                    id=f"{self.connector_id}:{source_id}",
                    tenant_id=tenant_id,
                    source=self.connector_id,
                    received_at=received_at,
                    payload=payload,
                )
            )
        return tuple(events), next_cursor

    def probe(self) -> bool:
        try:
            with httpx.Client(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.token}"} if self.token else None,
                verify=self.ca_bundle,
                cert=self.client_certificate,
                timeout=self.timeout_seconds,
                follow_redirects=False,
            ) as client:
                return client.get("/health").status_code == 200
        except (httpx.HTTPError, OSError):
            return False
