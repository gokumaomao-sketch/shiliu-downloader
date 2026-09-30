"""The local API must serve the extension and refuse web pages.

Every response used to carry ``Access-Control-Allow-Origin: *`` and the token
check passed whenever no token was configured — which is the default. So any page
the user visited could queue downloads on their machine and read their download
history straight off 127.0.0.1.

The hard constraint on the fix: the extensions people actually have are the store
builds (0.5.28 / 0.5.29). The desktop app auto-updates, the store extension does
not, so the lockdown must work WITHOUT the extension sending anything new. It
keys on ``Origin``, which the browser attaches itself — see
test_an_old_store_extension_request_still_works.

These tests run a real BrowserAPIServer and speak real HTTP to it.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

import pytest

from magic_downloader.browser_server import BrowserAPIServer, _origin_allowed


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def api():
    """A running server that records what reached its handlers."""
    calls: list[tuple[str, dict]] = []
    server = BrowserAPIServer(
        port=_free_port(),
        on_add=lambda data: calls.append(("add", data)) or {"id": "job1", "filename": "f.bin"},
        on_status=lambda: {"downloads": [{"filename": "private-video.mp4"}]},
        on_probe=lambda data: calls.append(("probe", data)) or {"formats": []},
        on_route=lambda data: calls.append(("route", data)) or {"folder": "D:/Video", "rule": "example.com"},
    )
    server.start()
    yield server, calls
    server.stop()


@pytest.fixture
def api_with_token():
    """A server that requires a token, for the 401 paths."""
    calls: list[tuple[str, dict]] = []
    server = BrowserAPIServer(
        port=_free_port(),
        on_add=lambda data: calls.append(("add", data)) or {"id": "j", "filename": "f"},
        on_status=lambda: {},
        token="s3cret",
    )
    server.start()
    yield server, calls
    server.stop()


def _request(server, path, method="GET", origin=None, body=None,
             content_type="application/json"):
    """Returns (status, body_text). A refused request surfaces as its status."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{server.base_url}{path}", data=data, method=method)
    if origin is not None:
        req.add_header("Origin", origin)
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), dict(exc.headers)


CHROME = "chrome-extension://pgiehelnpkakggoeglldnhmidapoeblb"
FIREFOX = "moz-extension://4f1e2a3b-0000-4000-8000-abcdefabcdef"
EVIL = "https://evil.example"


# ── the constraint: old store extensions must keep working ───────────────

def test_an_old_store_extension_request_still_works(api):
    """0.5.28/0.5.29 send no token and no custom header — only what the browser adds.

    Verified against git: in both versions every API call is made from
    background.js (the extension context), never from a content script, so the
    browser attaches Origin: chrome-extension://<id> on its own.
    """
    server, calls = api

    status, body, headers = _request(
        server, "/api/add", "POST", origin=CHROME,
        body={"url": "https://cdn.example/file.bin", "filename": "f.bin"},
    )

    assert status == 200, "the shipped store extension must not be broken"
    assert json.loads(body)["ok"] is True
    assert calls and calls[0][0] == "add"
    assert headers.get("Access-Control-Allow-Origin") == CHROME


def test_route_preview_is_extension_only(api):
    server, calls = api
    payload = {"url": "https://cdn.example/video.mp4", "page_url": "https://www.example.com/watch"}
    status, body, _ = _request(server, "/api/route", "POST", origin=CHROME, body=payload)
    assert status == 200 and json.loads(body)["folder"] == "D:/Video"
    status, _, _ = _request(server, "/api/route", "POST", origin=EVIL, body=payload)
    assert status == 403 and calls == [("route", payload)]


def test_firefox_extension_origin_works_too(api):
    server, _ = api
    status, _b, headers = _request(
        server, "/api/add", "POST", origin=FIREFOX,
        body={"url": "https://cdn.example/f.bin"},
    )
    assert status == 200
    assert headers.get("Access-Control-Allow-Origin") == FIREFOX


def test_ping_still_works_for_the_extension(api):
    server, _ = api
    status, body, _h = _request(server, "/api/ping", origin=CHROME)
    assert status == 200
    assert json.loads(body)["ok"] is True


def test_the_preflight_succeeds_for_an_extension(api):
    server, _ = api
    status, _b, headers = _request(server, "/api/add", "OPTIONS", origin=CHROME)
    assert status == 204
    assert headers.get("Access-Control-Allow-Origin") == CHROME


# ── the vulnerability: a web page must be refused ────────────────────────

def test_a_website_cannot_queue_a_download(api):
    server, calls = api

    status, _b, _h = _request(
        server, "/api/add", "POST", origin=EVIL,
        body={"url": "https://evil.example/payload.exe"},
    )

    assert status == 403
    assert calls == [], "the download must never reach the handler"


def test_a_website_cannot_read_the_download_history(api):
    server, _ = api
    status, body, _h = _request(server, "/api/status", origin=EVIL)
    assert status == 403
    assert "private-video.mp4" not in body


def test_a_website_cannot_probe(api):
    server, calls = api
    status, _b, _h = _request(
        server, "/api/probe", "POST", origin=EVIL, body={"url": "https://x.test/v.m3u8"}
    )
    assert status == 403
    assert calls == []


def test_the_preflight_is_refused_for_a_website(api):
    """Refusing here stops the page's fetch before the real request is sent."""
    server, _ = api
    status, _b, _h = _request(server, "/api/add", "OPTIONS", origin=EVIL)
    assert status == 403


def test_no_wildcard_is_ever_sent(api):
    """`*` let any origin read the reply — the root of the problem."""
    server, _ = api
    for origin in (CHROME, FIREFOX, None):
        _s, _b, headers = _request(server, "/api/ping", origin=origin)
        assert headers.get("Access-Control-Allow-Origin") != "*"


def test_an_allowed_response_varies_on_origin(api):
    server, _ = api
    _s, _b, headers = _request(server, "/api/ping", origin=CHROME)
    assert "Origin" in (headers.get("Vary") or "")


def test_a_form_post_without_json_is_refused(api):
    """A cross-origin form post is a "simple request": no preflight, so it would
    otherwise reach the handler and take effect even unreadable."""
    server, calls = api

    status, _b, _h = _request(
        server, "/api/add", "POST", origin=None,
        body={"url": "https://evil.example/payload.exe"},
        content_type="text/plain",
    )

    assert status == 415
    assert calls == []


def test_origin_null_is_refused():
    """A sandboxed iframe or file:// page sends Origin: null — not "absent"."""
    assert _origin_allowed("null") is False


def test_a_website_origin_is_refused_even_if_it_mentions_an_extension():
    assert _origin_allowed("https://chrome-extension.evil.test") is False
    assert _origin_allowed("http://evil.test/#chrome-extension://x") is False


def test_local_tooling_without_an_origin_still_works(api):
    """curl and the app's own tooling send no Origin; a browser always does."""
    server, _ = api
    status, body, _h = _request(server, "/api/ping", origin=None)
    assert status == 200
    assert json.loads(body)["ok"] is True


@pytest.mark.parametrize(
    "origin",
    ["chrome-extension://abc", "moz-extension://abc", "safari-web-extension://abc", ""],
)
def test_allowed_origins(origin):
    assert _origin_allowed(origin) is True


@pytest.mark.parametrize(
    "origin",
    ["https://evil.test", "http://localhost:3000", "http://127.0.0.1:8080", "null",
     "file://", "https://google.com"],
)
def test_refused_origins(origin):
    assert _origin_allowed(origin) is False


# ── found by an adversarial review of the first version of this fix ──────

def test_a_refused_response_carries_no_allow_origin(api):
    """A 403 must not hand the caller permission to read it."""
    server, _ = api
    _s, _b, headers = _request(server, "/api/ping", origin=EVIL)
    assert "Access-Control-Allow-Origin" not in headers


def test_a_real_request_with_origin_null_is_refused(api):
    """End-to-end, not just the helper: sandboxed iframes send exactly this."""
    server, calls = api
    status, _b, _h = _request(
        server, "/api/add", "POST", origin="null", body={"url": "https://x.test/a.bin"}
    )
    assert status == 403
    assert calls == []


def test_refusals_vary_on_origin(api):
    server, _ = api
    _s, _b, headers = _request(server, "/api/ping", origin=EVIL)
    assert "Origin" in (headers.get("Vary") or "")


def test_no_origin_response_omits_allow_origin_entirely(api):
    """Tighter than "not *": there is nothing to reflect, so send nothing."""
    server, _ = api
    _s, _b, headers = _request(server, "/api/ping", origin=None)
    assert "Access-Control-Allow-Origin" not in headers


def test_a_wrong_token_gives_a_readable_401_not_a_socket_error(api_with_token):
    """Undrained bodies made Windows reset the connection, so the extension
    reported "cannot reach the app" to a user who had merely mistyped a token."""
    server, _calls = api_with_token
    req = urllib.request.Request(
        f"{server.base_url}/api/add",
        data=json.dumps({"url": "https://x.test/a.bin"}).encode(),
        method="POST",
    )
    req.add_header("Origin", CHROME)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Magic-Token", "WRONG")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 401, "the refusal must arrive as an HTTP status"


def test_the_right_token_still_works(api_with_token):
    server, calls = api_with_token
    req = urllib.request.Request(
        f"{server.base_url}/api/add",
        data=json.dumps({"url": "https://x.test/a.bin"}).encode(),
        method="POST",
    )
    req.add_header("Origin", CHROME)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Magic-Token", "s3cret")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
    assert calls and calls[0][0] == "add"


# ── DNS rebinding: the Origin gate cannot see it, the Host header can ────

def test_a_rebound_attacker_domain_is_refused(api):
    """After rebinding, the page is SAME-ORIGIN with us, so it sends no Origin
    at all — Host is the only thing that still names the attacker."""
    server, _ = api
    req = urllib.request.Request(f"{server.base_url}/api/status")
    req.add_header("Host", "evil.test:7373")     # no Origin, exactly as rebinding gives
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            status, body = resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read().decode()

    assert status == 403
    assert "private-video.mp4" not in body


def test_a_rebound_domain_cannot_queue_a_download(api):
    server, calls = api
    req = urllib.request.Request(
        f"{server.base_url}/api/add",
        data=json.dumps({"url": "https://evil.test/x.exe"}).encode(),
        method="POST",
    )
    req.add_header("Host", "evil.test:7373")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 403
    assert calls == []


def test_the_local_host_header_the_extension_sends_is_accepted(api):
    """background.js builds http://127.0.0.1:<port>, so this must pass."""
    server, _ = api
    status, _b, _h = _request(server, "/api/ping", origin=CHROME)
    assert status == 200


import pytest as _pytest


@_pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:7373", "localhost:7373",
                                   "[::1]:7373", "::1", ""])
def test_local_hosts_accepted(host):
    from magic_downloader.browser_server import _host_is_local
    assert _host_is_local(host) is True


@_pytest.mark.parametrize("host", ["evil.test", "evil.test:7373", "127.0.0.1.evil.test",
                                   "attacker.com:80"])
def test_foreign_hosts_refused(host):
    from magic_downloader.browser_server import _host_is_local
    assert _host_is_local(host) is False
