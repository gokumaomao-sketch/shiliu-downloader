"""Local HTTP API for browser extension integration (browser capture).

Binds only to 127.0.0.1. The Chrome/Edge extension posts download requests here.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from magic_downloader import __version__
from magic_downloader.paths import DATA_DIR
from magic_downloader.media.ffmpeg import has_ffmpeg

AddHandler = Callable[[dict[str, Any]], dict[str, Any]]
StatusHandler = Callable[[], dict[str, Any]]
ProbeHandler = Callable[[dict[str, Any]], dict[str, Any]]
RouteHandler = Callable[[dict[str, Any]], dict[str, Any]]

#: Schemes a browser uses for an extension's own pages and workers. Requests from
#: our extension carry one of these in Origin — the BROWSER sets that header, not
#: our code, so this works for every extension version ever published and needs
#: no change on the extension side.
_EXTENSION_ORIGIN_SCHEMES = (
    "chrome-extension://",
    "moz-extension://",
    "safari-web-extension://",
)

#: Host values a legitimate caller can present. The extension builds its URL as a
#: literal ``http://127.0.0.1:<port>`` (only the port is configurable), so this
#: costs it nothing — and it is what stops DNS rebinding, where a page served
#: from an attacker domain that re-resolves to 127.0.0.1 becomes *same-origin*
#: with us and therefore sends no Origin at all.
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})


def _origin_allowed(origin: str) -> bool:
    """May a caller sending this Origin use the API?

    The API used to answer every origin with ``Access-Control-Allow-Origin: *``,
    so any page the user happened to be visiting could queue downloads and read
    their download history straight off 127.0.0.1.

    Allowed:
      * an extension origin — that is our caller;
      * no Origin at all. Careful: this does NOT mean "cannot be a web page".
        Origin is only attached when the response tainting is CORS or the method
        is not GET/HEAD, so ``<img>``, ``<script>``, a navigation or a
        ``mode:"no-cors"`` GET from any page arrives without one. It is allowed
        because curl and local tooling need it and because the routes reachable
        that way have no side effects — POST is separately gated on a JSON
        content type. Any future state-changing route must require a real
        extension origin rather than relying on this.
    ``Origin: null`` (sandboxed iframe, file://) is a value, not an absence, and
    is refused, as is every http(s) website.
    """
    if not origin:
        return True
    return origin.lower().startswith(_EXTENSION_ORIGIN_SCHEMES)


def _host_is_local(host_header: str) -> bool:
    """True when the request was addressed to this machine by a local name.

    A rebound attacker domain (``evil.test`` pointing at 127.0.0.1) still sends
    ``Host: evil.test:7373``, which is what gives it away — the Origin check
    cannot, because after rebinding the browser considers the request
    same-origin and omits Origin entirely.
    """
    host = (host_header or "").strip()
    if host.startswith("["):                      # [::1]:7373
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:                    # host:port
        host = host.split(":", 1)[0]
    return host.lower() in _LOCAL_HOSTS


class BrowserAPIServer:
    """Background localhost server for the browser extension."""

    def __init__(
        self,
        port: int,
        on_add: AddHandler,
        on_status: StatusHandler | None = None,
        token: str = "",
        on_probe: ProbeHandler | None = None,
        on_route: RouteHandler | None = None,
    ) -> None:
        self.port = int(port)
        self.on_add = on_add
        self.on_status = on_status or (lambda: {})
        self.on_probe = on_probe
        self.on_route = on_route
        self.token = token or ""
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.running = False
        self.last_error = ""
        #: The last Origin/Host turned away, for diagnosing a caller that the
        #: allowlist unexpectedly refuses (surfaced in Options -> Browser).
        self.last_refused_origin = ""

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        if self.running:
            return
        handler = self._make_handler()
        try:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
            # Avoid long TIME_WAIT issues on rapid restart during dev
            self._httpd.daemon_threads = True
        except OSError as exc:
            self.last_error = str(exc)
            self.running = False
            raise
        self.running = True
        self.last_error = ""
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="browser-api", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        self._thread = None

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            #: StreamRequestHandler.setup() honours this. Without it a caller
            #: that announces a body and then stalls parks a worker thread
            #: forever, and the server spawns one thread per connection.
            timeout = 30

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                # Keep console quiet; errors still useful for debug if needed
                return

            def _cors(self) -> None:
                origin = self.headers.get("Origin", "")
                # Reflect the caller's own origin instead of "*", so only the
                # extension that asked can read the reply. Vary: Origin keeps any
                # cache from serving one caller's response to another.
                if origin and _origin_allowed(origin):
                    self.send_header("Access-Control-Allow-Origin", origin)
                # Unconditional: the status AND the body depend on Origin (403 vs
                # 200), so every response varies on it, refusals included.
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header(
                    "Access-Control-Allow-Headers",
                    "Content-Type, Authorization, X-Magic-Token",
                )

            def _drain_body(self) -> None:
                """Read and discard the request body before refusing it.

                Answering a POST without consuming what the client is still
                sending makes Windows abort the connection, so the caller gets a
                socket error instead of the 403/415 explaining the refusal.
                Capped so an oversized body cannot be used to tie up a thread.
                """
                try:
                    declared = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    self.close_connection = True
                    return
                if self.headers.get("Transfer-Encoding"):
                    self.close_connection = True   # chunked: cannot drain by length
                    return
                remaining = min(declared, 1 << 20)
                if declared > remaining:
                    # Too big to swallow. Closing marks the leftovers as nobody's
                    # business, so they can never be parsed as a following
                    # request if this handler is ever put on HTTP/1.1 keep-alive.
                    self.close_connection = True
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        self.close_connection = True
                        break
                    remaining -= len(chunk)

            def _reject_foreign_origin(self) -> bool:
                """True (and answers 403) when a web page is calling us."""
                origin = self.headers.get("Origin", "")
                if not _host_is_local(self.headers.get("Host", "")):
                    # A rebound attacker domain resolves here but still names
                    # itself in Host; after rebinding it is same-origin, so the
                    # Origin check alone would wave it through.
                    server_ref.last_refused_origin = self.headers.get("Host", "")
                    self._drain_body()
                    self._json(403, {"ok": False, "error": "Forbidden host"})
                    return True
                if _origin_allowed(origin):
                    return False
                # Remember it: this whole gate rests on which Origin a browser
                # attaches, so if some build ever sends one we do not expect, the
                # symptom is "the extension stopped working" with no evidence.
                # Options -> Browser can surface this.
                server_ref.last_refused_origin = origin
                self._drain_body()
                self._json(403, {"ok": False, "error": "Forbidden origin"})
                return True

            def _json_request(self) -> bool:
                """True when the POST body is declared as JSON.

                A cross-origin form post using text/plain or
                x-www-form-urlencoded is a "simple request": no preflight, so it
                reaches the handler and takes effect even though the page can
                never read the answer. Our extension has always sent
                application/json, so requiring it costs nothing and closes that.
                """
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                return ctype == "application/json"

            def _check_token(self) -> bool:
                if not server_ref.token:
                    return True
                auth = self.headers.get("Authorization", "")
                header_token = self.headers.get("X-Magic-Token", "")
                if auth.startswith("Bearer "):
                    header_token = auth[7:].strip() or header_token
                return header_token == server_ref.token

            def _json(self, code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self._cors()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self) -> None:  # noqa: N802
                # Refusing the preflight is what stops a page's fetch() before
                # the real request is ever sent.
                if (not _origin_allowed(self.headers.get("Origin", ""))
                        or not _host_is_local(self.headers.get("Host", ""))):
                    self.send_response(403)
                    self.send_header("Vary", "Origin")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                if self._reject_foreign_origin():
                    return
                path = urlparse(self.path).path.rstrip("/") or "/"
                if path in ("/api/ping", "/ping"):
                    self._json(
                        200,
                        {
                            "ok": True,
                            "name": "拾流下载器",
                            "version": __version__,
                            "port": server_ref.port,
                            "ffmpeg": has_ffmpeg(),
                        },
                    )
                    return
                if path in ("/api/status", "/status"):
                    if not self._check_token():
                        self._json(401, {"ok": False, "error": "Unauthorized"})
                        return
                    try:
                        self._json(200, {"ok": True, **server_ref.on_status()})
                    except Exception as exc:  # noqa: BLE001
                        self._json(500, {"ok": False, "error": str(exc)})
                    return
                self._json(404, {"ok": False, "error": "Not found"})

            def do_POST(self) -> None:  # noqa: N802
                if self._reject_foreign_origin():
                    return
                path = urlparse(self.path).path.rstrip("/") or "/"
                if path not in ("/api/add", "/add", "/api/probe", "/probe", "/api/route"):
                    self._drain_body()
                    self._json(404, {"ok": False, "error": "Not found"})
                    return
                if not self._json_request():
                    self._drain_body()
                    self._json(415, {"ok": False, "error": "Expected application/json"})
                    return
                if not self._check_token():
                    # Drain here too, or Windows resets the connection and the
                    # extension reports "cannot reach the app" — sending a user
                    # with a mistyped token hunting a problem that isn't there,
                    # while the app runs in front of them.
                    self._drain_body()
                    self._json(401, {"ok": False, "error": "Unauthorized"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    self._json(400, {"ok": False, "error": "Invalid Content-Length"})
                    return
                length = min(max(length, 0), 8 << 20)
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    data = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self._json(400, {"ok": False, "error": "Invalid JSON"})
                    return
                if not isinstance(data, dict):
                    self._json(400, {"ok": False, "error": "Expected JSON object"})
                    return
                url = str(data.get("url") or "").strip()
                if not url or urlparse(url).scheme not in ("http", "https"):
                    self._json(400, {"ok": False, "error": "Invalid url"})
                    return

                if path in ("/api/probe", "/probe"):
                    if server_ref.on_probe is None:
                        self._json(501, {"ok": False, "error": "Probe not supported"})
                        return
                    try:
                        result = server_ref.on_probe(data)
                        self._json(200, {"ok": True, **result})
                    except Exception as exc:  # noqa: BLE001
                        self._json(500, {"ok": False, "error": str(exc)})
                    return

                if path == "/api/route":
                    if server_ref.on_route is None:
                        self._json(501, {"ok": False, "error": "Route preview not supported"})
                        return
                    try:
                        self._json(200, {"ok": True, **server_ref.on_route(data)})
                    except Exception as exc:  # noqa: BLE001
                        self._json(500, {"ok": False, "error": str(exc)})
                    return

                try:
                    result = server_ref.on_add(data)
                    self._json(200, {"ok": True, **result})
                except Exception as exc:  # noqa: BLE001
                    self._json(500, {"ok": False, "error": str(exc)})

        return Handler
