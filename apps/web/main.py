from __future__ import annotations

import json
import mimetypes
import os
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

MAX_PROXY_BODY_BYTES = 2 * 1024 * 1024
PROXY_PREFIXES = ("/health/", "/version", "/v1/", "/openapi.json")
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


@dataclass(frozen=True, slots=True)
class WebSettings:
    environment: str
    host: str
    port: int
    static_root: Path
    api_upstream: str
    oidc_issuer: str | None
    oidc_client_id: str | None
    oidc_audience: str | None
    oidc_scopes: str
    dev_identity: dict[str, str] | None
    release_id: str = "local-candidate"

    @classmethod
    def from_env(cls) -> WebSettings:
        environment = os.getenv("COMPUTEWEAVER_ENV", "development").lower()
        default_root = Path("/app/web") if Path("/app/web").is_dir() else Path(__file__).with_name("dist")
        settings = cls(
            environment=environment,
            host=os.getenv("COMPUTEWEAVER_WEB_HOST", "0.0.0.0"),  # noqa: S104 - container listener
            port=int(os.getenv("COMPUTEWEAVER_WEB_PORT", "8080")),
            static_root=Path(os.getenv("COMPUTEWEAVER_WEB_STATIC_ROOT", str(default_root))).resolve(),
            api_upstream=os.getenv("COMPUTEWEAVER_WEB_API_UPSTREAM", "http://127.0.0.1:8000").rstrip("/"),
            oidc_issuer=os.getenv("COMPUTEWEAVER_OIDC_ISSUER"),
            oidc_client_id=os.getenv("COMPUTEWEAVER_WEB_OIDC_CLIENT_ID"),
            oidc_audience=os.getenv("COMPUTEWEAVER_OIDC_AUDIENCE"),
            oidc_scopes=os.getenv("COMPUTEWEAVER_WEB_OIDC_SCOPES", "openid profile email"),
            dev_identity=(
                {
                    "tenant_id": os.getenv("COMPUTEWEAVER_WEB_DEV_TENANT", "tenant-local"),
                    "actor_id": os.getenv("COMPUTEWEAVER_WEB_DEV_ACTOR", "operator-local"),
                    "roles": os.getenv("COMPUTEWEAVER_WEB_DEV_ROLES", "admin,operator"),
                }
                if environment in {"development", "simulator", "test"}
                else None
            ),
            release_id=os.getenv("COMPUTEWEAVER_RELEASE_ID", "local-candidate"),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.environment not in {"development", "simulator", "test", "staging", "production"}:
            raise ValueError("unknown web environment")
        if not 1 <= self.port <= 65535:
            raise ValueError("web port is invalid")
        if urlsplit(self.api_upstream).scheme not in {"http", "https"}:
            raise ValueError("web API upstream must use HTTP or HTTPS")
        if not (self.static_root / "index.html").is_file():
            raise ValueError("web static build is unavailable")
        if self.environment == "production":
            if not self.oidc_issuer or not self.oidc_issuer.startswith("https://"):
                raise ValueError("production web console requires an HTTPS OIDC issuer")
            if not self.oidc_client_id:
                raise ValueError("production web console requires an OIDC client ID")

    def public_config(self) -> dict[str, object]:
        return {
            "environment": self.environment,
            "oidc": {
                "issuer": self.oidc_issuer,
                "client_id": self.oidc_client_id,
                "audience": self.oidc_audience,
                "scopes": self.oidc_scopes,
            },
            "dev_identity": self.dev_identity,
            "release_id": self.release_id,
        }


class WebHandler(BaseHTTPRequestHandler):
    settings: WebSettings
    server_version = "ComputeWeaverWeb/0.1"

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/web-health/live":
            self._json(HTTPStatus.OK, {"status": "live"})
        elif path == "/web-config.json":
            self._json(HTTPStatus.OK, self.settings.public_config(), cache_control="no-store")
        elif self._is_proxy_path(path):
            self._proxy()
        else:
            self._static(path)

    def do_POST(self) -> None:  # noqa: N802
        self._proxy_or_reject()

    def do_PUT(self) -> None:  # noqa: N802
        self._proxy_or_reject()

    def do_PATCH(self) -> None:  # noqa: N802
        self._proxy_or_reject()

    def do_DELETE(self) -> None:  # noqa: N802
        self._proxy_or_reject()

    @staticmethod
    def _is_proxy_path(path: str) -> bool:
        return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in PROXY_PREFIXES)

    def _proxy_or_reject(self) -> None:
        if not self._is_proxy_path(urlsplit(self.path).path):
            self._json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"})
            return
        self._proxy()

    def _proxy(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_content_length"})
            return
        if length < 0 or length > MAX_PROXY_BODY_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request_too_large"})
            return
        body = self.rfile.read(length) if length else None
        headers = {key: value for key, value in self.headers.items() if key.lower() not in HOP_BY_HOP}
        try:
            with httpx.Client(
                base_url=self.settings.api_upstream,
                timeout=httpx.Timeout(30, connect=5),
                follow_redirects=False,
            ) as client:
                response = client.request(self.command, self.path, headers=headers, content=body)
        except httpx.HTTPError:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": "api_unavailable"})
            return
        self.send_response(response.status_code)
        for key, value in response.headers.items():
            if key.lower() not in HOP_BY_HOP:
                self.send_header(key, value)
        self._security_headers()
        self.send_header("Content-Length", str(len(response.content)))
        self.end_headers()
        self.wfile.write(response.content)

    def _static(self, request_path: str) -> None:
        relative = unquote(request_path).lstrip("/") or "index.html"
        candidate = (self.settings.static_root / relative).resolve()
        try:
            candidate.relative_to(self.settings.static_root)
        except ValueError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "asset_not_found"})
            return
        if not candidate.is_file():
            candidate = self.settings.static_root / "index.html"
        if not candidate.is_file():
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "web_build_unavailable"})
            return
        content = candidate.read_bytes()
        media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", media_type)
        cache_control = "no-cache" if candidate.name == "index.html" else "public, max-age=31536000, immutable"
        self.send_header("Cache-Control", cache_control)
        self._security_headers()
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _json(self, status: HTTPStatus, body: dict[str, object], *, cache_control: str = "no-store") -> None:
        content = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", cache_control)
        self._security_headers()
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _security_headers(self) -> None:
        issuer_origin = ""
        if self.settings.oidc_issuer:
            parsed = urlsplit(self.settings.oidc_issuer)
            issuer_origin = f" {parsed.scheme}://{parsed.netloc}"
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            f"connect-src 'self'{issuer_origin}; "
            "img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    def log_message(self, format: str, *args: object) -> None:
        super().log_message(format, *args)


def main() -> None:
    settings = WebSettings.from_env()
    WebHandler.settings = settings
    server = ThreadingHTTPServer((settings.host, settings.port), WebHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
