"""Browser request isolation and response headers, including behind a tunnel."""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _origin(value: str, *, origin_only: bool = False) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(char.isspace() for char in value)
            or (origin_only and (parsed.path or parsed.query or parsed.fragment))
        ):
            return None
        port = parsed.port
        return parsed.scheme, parsed.hostname.lower(), (
            port if port is not None else 443 if parsed.scheme == "https" else 80
        )
    except ValueError:
        return None


class SecurityMiddleware:
    """Reject cross-origin writes before reading uploads or entering routes.

    Browser provenance is checked whenever supplied. Headerless non-browser
    clients remain supported; this is not authentication. A configured public
    origin handles TLS termination without trusting arbitrary forwarded headers.
    """

    def __init__(
        self, app: ASGIApp, public_origin: str = "",
        csp_exempt_paths: frozenset[str] = frozenset(),
    ) -> None:
        self.app = app
        self.csp_exempt_paths = csp_exempt_paths
        self.public_origin = _origin(public_origin, origin_only=True) if public_origin else None
        if public_origin and self.public_origin is None:
            raise ValueError("GEOFLOW_PUBLIC_ORIGIN must be an HTTP(S) origin")

    def _allowed(self, scope: Scope, headers: Headers) -> bool:
        if any(len(headers.getlist(name)) > 1 for name in ("host", "origin", "referer", "sec-fetch-site")):
            return False
        site = headers.get("sec-fetch-site", "").lower()
        if site and site != "same-origin":
            return False
        origin = headers.get("origin")
        referer = headers.get("referer")
        source = origin if origin is not None else referer
        if source is None:
            # CLI integrations have no ambient browser authentication.
            return not site and "cookie" not in headers
        candidate = _origin(source, origin_only=origin is not None)
        target = _origin(f"{scope['scheme']}://{headers.get('host', '')}")
        if candidate is None or target is None:
            return False
        if candidate == target:
            return True
        public_target = _origin(f"{candidate[0]}://{headers.get('host', '')}")
        return candidate == self.public_origin and candidate == public_target

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = {
                    name: value for name, value in SECURITY_HEADERS.items()
                    if name != "Content-Security-Policy" or scope["path"] not in self.csp_exempt_paths
                }
                # Replace any defaults consistently on HTML, JSON and downloads.
                protected = {name.lower().encode() for name in SECURITY_HEADERS}
                message["headers"] = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in protected
                ] + [
                    (name.lower().encode(), value.encode())
                    for name, value in response_headers.items()
                ]
            await send(message)

        if scope["method"] not in SAFE_METHODS and not self._allowed(scope, Headers(scope=scope)):
            response = JSONResponse({"detail": "Origine de la requête refusée."}, status_code=403)
            await response(scope, receive, send_with_headers)
            return
        await self.app(scope, receive, send_with_headers)
