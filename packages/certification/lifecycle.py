from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from .evidence import verify_evidence, write_evidence
from .service import CertificationResult, certificate_from_document
from .signing import verify_release_token

RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EVENT_TYPES = frozenset({"CertificationStarted", "GateFailed", "ReleaseCertified", "CertificationRevoked"})


@dataclass(frozen=True, slots=True)
class CertificationEvent:
    event_type: str
    release_id: str
    occurred_at: datetime
    actor_id: str
    payload: dict[str, Any]
    previous_hash: str
    event_hash: str


class CertificationRepository:
    """Atomic evidence-backed certification lifecycle repository.

    The repository is suitable for a mounted encrypted evidence volume. Every mutable index is
    protected by a SHA-256 sidecar and every lifecycle event forms an append-only hash chain.
    """

    def __init__(self, evidence_root: Path) -> None:
        self.evidence_root = evidence_root
        self.root = evidence_root / "B20"
        self._lock = RLock()

    @staticmethod
    def _validate_release_id(release_id: str) -> str:
        if not RELEASE_ID.fullmatch(release_id):
            raise ValueError("release_id contains unsupported characters")
        return release_id

    def _path(self, category: str, release_id: str) -> Path:
        return self.root / category / f"{self._validate_release_id(release_id)}.json"

    @staticmethod
    def _load_verified(path: Path) -> dict[str, Any]:
        integrity, error = verify_evidence(path)
        if not integrity:
            raise ValueError(f"certification repository integrity check failed: {error}")
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("certification repository document must be an object")
        return document

    def append_event(
        self,
        event_type: str,
        *,
        release_id: str,
        actor_id: str,
        payload: dict[str, Any],
        occurred_at: datetime | None = None,
    ) -> CertificationEvent:
        if event_type not in EVENT_TYPES:
            raise ValueError("unsupported certification lifecycle event")
        release_id = self._validate_release_id(release_id)
        timestamp = occurred_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("certification event timestamp must be timezone-aware")
        with self._lock:
            path = self._path("events", release_id)
            events: list[dict[str, Any]] = []
            if path.exists():
                document = self._load_verified(path)
                events = [dict(item) for item in document.get("events", []) if isinstance(item, dict)]
            previous = str(events[-1]["event_hash"]) if events else "GENESIS"
            body = {
                "event_type": event_type,
                "release_id": release_id,
                "occurred_at": timestamp.isoformat(),
                "actor_id": actor_id,
                "payload": payload,
                "previous_hash": previous,
            }
            digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            event = CertificationEvent(
                event_type,
                release_id,
                timestamp,
                actor_id,
                payload,
                previous,
                digest,
            )
            events.append(json.loads(json.dumps(asdict(event), default=str)))
            write_evidence(
                path,
                {"status": "PASS", "release_id": release_id, "events": events},
                command=f"certify event {event_type}",
                suite_name="certification-lifecycle-events",
            )
            return event

    def save_run(self, result: CertificationResult, *, actor_id: str) -> Path:
        with self._lock:
            path = self._path("runs", result.release_id)
            if self._path("certificates", result.release_id).exists():
                raise FileExistsError("a published release cannot be recertified in place")
            if path.exists():
                existing = certificate_from_document(self._load_verified(path))
                if existing.certificate_hash != result.certificate_hash:
                    raise FileExistsError("a different certification run already exists for this release")
                return path
            self.append_event(
                "CertificationStarted",
                release_id=result.release_id,
                actor_id=actor_id,
                payload={"commit": result.commit, "certificate_hash": result.certificate_hash},
                occurred_at=result.generated_at,
            )
            for gate in result.gates:
                if not gate.passed:
                    self.append_event(
                        "GateFailed",
                        release_id=result.release_id,
                        actor_id=actor_id,
                        payload={"gate": gate.name, "reason": gate.reason, "evidence": gate.evidence},
                        occurred_at=result.generated_at,
                    )
            write_evidence(
                path,
                json.loads(json.dumps(asdict(result), default=str)),
                command="certify run",
                suite_name="certification-run",
            )
            return path

    def load_run(self, release_id: str) -> CertificationResult:
        return certificate_from_document(self._load_verified(self._path("runs", release_id)))

    def publish(
        self,
        result: CertificationResult,
        *,
        actor_id: str,
        public_key_file: Path,
    ) -> Path:
        if result.status != "CERTIFIED" or not result.signature:
            raise ValueError("publishing requires a signed CERTIFIED result")
        verify_release_token(
            result.signature,
            public_key_file=public_key_file,
            expected_certificate=result,
        )
        with self._lock:
            run = self.load_run(result.release_id)
            if run.certificate_hash != result.certificate_hash:
                raise ValueError("published certificate does not match the saved certification run")
            if self.revocation(result.release_id):
                raise ValueError("a revoked release cannot be republished")
            path = self._path("certificates", result.release_id)
            if path.exists():
                existing = certificate_from_document(self._load_verified(path))
                if existing.certificate_hash != result.certificate_hash:
                    raise FileExistsError("a different certificate already exists for this release")
                return path
            write_evidence(
                path,
                json.loads(json.dumps(asdict(result), default=str)),
                command="certify release",
                suite_name="production-release-certificate",
            )
            self.append_event(
                "ReleaseCertified",
                release_id=result.release_id,
                actor_id=actor_id,
                payload={"commit": result.commit, "certificate_hash": result.certificate_hash},
            )
            return path

    def get(self, release_id: str) -> CertificationResult:
        path = self._path("certificates", release_id)
        if not path.exists():
            path = self._path("runs", release_id)
        return certificate_from_document(self._load_verified(path))

    def revoke(self, release_id: str, *, actor_id: str, reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("revocation reason is required")
        certificate_path = self._path("certificates", release_id)
        if not certificate_path.exists():
            raise ValueError("only a published certificate can be revoked")
        certificate = certificate_from_document(self._load_verified(certificate_path))
        if certificate.status != "CERTIFIED" or not certificate.signature:
            raise ValueError("only a signed CERTIFIED release can be revoked")
        path = self._path("revocations", release_id)
        with self._lock:
            if path.exists():
                existing = self._load_verified(path)
                if existing.get("certificate_hash") != certificate.certificate_hash:
                    raise ValueError("revocation does not match the current certificate")
                return existing
            document = {
                "status": "REVOKED",
                "release_id": release_id,
                "certificate_hash": certificate.certificate_hash,
                "revoked_at": datetime.now(UTC).isoformat(),
                "actor_id": actor_id,
                "reason": reason.strip(),
            }
            write_evidence(
                path,
                document,
                command="certify revoke",
                suite_name="certification-revocation",
            )
            self.append_event(
                "CertificationRevoked",
                release_id=release_id,
                actor_id=actor_id,
                payload={"certificate_hash": certificate.certificate_hash, "reason": reason.strip()},
            )
            self._write_revocation_registry()
            return self._load_verified(path)

    def revocation(self, release_id: str) -> dict[str, Any] | None:
        path = self._path("revocations", release_id)
        return self._load_verified(path) if path.exists() else None

    def events(self, release_id: str) -> tuple[CertificationEvent, ...]:
        path = self._path("events", release_id)
        if not path.exists():
            return ()
        document = self._load_verified(path)
        return tuple(
            CertificationEvent(
                event_type=str(item["event_type"]),
                release_id=str(item["release_id"]),
                occurred_at=datetime.fromisoformat(str(item["occurred_at"])),
                actor_id=str(item["actor_id"]),
                payload=dict(item.get("payload", {})),
                previous_hash=str(item["previous_hash"]),
                event_hash=str(item["event_hash"]),
            )
            for item in document.get("events", [])
            if isinstance(item, dict)
        )

    def verify_event_chain(self, release_id: str) -> bool:
        previous = "GENESIS"
        for event in self.events(release_id):
            body = {
                "event_type": event.event_type,
                "release_id": event.release_id,
                "occurred_at": event.occurred_at.isoformat(),
                "actor_id": event.actor_id,
                "payload": event.payload,
                "previous_hash": event.previous_hash,
            }
            digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            if event.previous_hash != previous or event.event_hash != digest:
                return False
            previous = event.event_hash
        return True

    def _write_revocation_registry(self) -> Path:
        entries: list[dict[str, Any]] = []
        for path in sorted((self.root / "revocations").glob("*.json")):
            entries.append(self._load_verified(path))
        destination = self.root / "revocations.json"
        write_evidence(
            destination,
            {"status": "PASS", "revocations": entries},
            command="certify revocation-registry",
            suite_name="certification-revocation-registry",
        )
        return destination

    def view(self, release_id: str) -> dict[str, Any]:
        published = self._path("certificates", release_id).exists()
        certificate = self.get(release_id)
        document = json.loads(json.dumps(asdict(certificate), default=str))
        document["published"] = published
        if not published and certificate.status == "CERTIFIED":
            document["status"] = "READY_FOR_RELEASE"
        revocation = self.revocation(release_id)
        if revocation:
            document["status"] = "REVOKED"
            document["revocation"] = revocation
        return document
