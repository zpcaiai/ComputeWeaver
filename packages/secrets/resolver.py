from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,80}$")
DEFAULT_CONNECTOR_SECRET_ROOT = Path("/var/run/secrets/computeweaver/connectors")


@dataclass(frozen=True, slots=True)
class CredentialResolver:
    """Resolve connector credentials without accepting arbitrary environment or file reads."""

    file_root: Path = DEFAULT_CONNECTOR_SECRET_ROOT
    environment_prefix: str = "COMPUTEWEAVER_CONNECTOR_SECRET_"

    @classmethod
    def from_env(cls) -> CredentialResolver:
        return cls(Path(os.getenv("COMPUTEWEAVER_CONNECTOR_SECRET_ROOT", str(DEFAULT_CONNECTOR_SECRET_ROOT))).resolve())

    def resolve(self, reference: str) -> str:
        if reference.startswith("secret://"):
            name = reference.removeprefix("secret://")
            if not SECRET_NAME.fullmatch(name):
                raise ValueError("connector secret name is invalid")
            value = os.getenv(f"{self.environment_prefix}{name}")
            if not value:
                raise PermissionError("connector credential is unavailable")
            return value
        if reference.startswith("file://"):
            return self.resolve_file(reference).read_text(encoding="utf-8").strip()
        raise ValueError("credential_ref must use secret:// or file://")

    def resolve_file(self, reference: str) -> Path:
        relative = reference.removeprefix("file://")
        if not relative or Path(relative).is_absolute():
            raise ValueError("connector secret file reference must be relative")
        root = self.file_root.resolve()
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise PermissionError("connector secret path escapes the configured root") from error
        if not candidate.is_file():
            raise PermissionError("connector credential file is unavailable")
        return candidate
