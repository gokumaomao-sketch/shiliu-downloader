"""A web page must not be able to shut the app down.

The single-instance control socket on 127.0.0.1 dispatched on a SUBSTRING of the
first 64 bytes it received (``if "quit" in data``). A browser request line is
"GET /quit HTTP/1.1\\r\\nHost: 127.0.0.1:47923..." — which contains "quit". So any
page the user visited could run

    <img src="http://127.0.0.1:47923/quit">

and the app would exit, dropping every download in flight. No CORS, no preflight
and nothing to click: this is a raw socket, so the side effect happens the moment
the bytes arrive. `/show` likewise let any site raise and focus the window.

The commands are now matched exactly, which the real sender already satisfies.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from magic_downloader.single_instance import SingleInstance


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def listener():
    """A running control listener that records the commands it acted on."""
    fired: list[str] = []
    si = SingleInstance(port=_free_port())
    assert si.acquire(takeover=False), "test needs the port"
    si.start_listener(
        on_quit=lambda: fired.append("quit"),
        on_show=lambda: fired.append("show"),
    )
    yield si, fired
    si.close()


def _send(si: SingleInstance, raw: bytes) -> None:
    with socket.create_connection(("127.0.0.1", si.port), timeout=2) as c:
        c.sendall(raw)
        c.settimeout(2)
        try:
            c.recv(16)
        except OSError:
            pass
    # the handler acts after closing the connection
    for _ in range(40):
        time.sleep(0.02)


# ── the attack ───────────────────────────────────────────────────────────

def test_a_browser_request_for_slash_quit_does_not_stop_the_app(listener):
    si, fired = listener

    _send(si, b"GET /quit HTTP/1.1\r\nHost: 127.0.0.1:47923\r\nAccept: */*\r\n\r\n")

    assert fired == [], "a web page must not be able to shut the app down"


def test_a_browser_request_for_slash_show_does_not_raise_the_window(listener):
    si, fired = listener

    _send(si, b"GET /show HTTP/1.1\r\nHost: 127.0.0.1:47923\r\n\r\n")

    assert fired == []


def test_the_word_quit_anywhere_in_a_request_is_ignored(listener):
    si, fired = listener

    _send(si, b"POST /a/quit/b HTTP/1.1\r\nHost: x\r\nOrigin: https://evil.test\r\n\r\n")
    _send(si, b"GET / HTTP/1.1\r\nReferer: https://evil.test/?x=quit\r\n\r\n")

    assert fired == []


def test_junk_is_ignored(listener):
    si, fired = listener

    _send(si, b"\x00\xff nonsense \r\n")
    _send(si, b"")

    assert fired == []


# ── the real sender must keep working ────────────────────────────────────

def test_the_apps_own_quit_command_still_works(listener):
    """_send_command writes exactly "quit\\n"."""
    si, fired = listener

    _send(si, b"quit\n")

    assert fired == ["quit"]


def test_the_apps_own_show_command_still_works(listener):
    si, fired = listener

    _send(si, b"show\n")

    assert fired == ["show"]


def test_send_command_round_trip(listener):
    """Drive the genuine sender, not a hand-written payload."""
    si, fired = listener

    assert si._send_command("show") is True
    for _ in range(40):
        time.sleep(0.02)

    assert fired == ["show"]


def test_a_second_instance_can_still_take_over(listener):
    """acquire(takeover=True) tells the running instance to quit — the whole
    point of the socket. It must survive the tightened matching."""
    si, fired = listener

    other = SingleInstance(port=si.port)
    other.acquire(takeover=True, wait=0.5)
    for _ in range(40):
        time.sleep(0.02)

    assert fired == ["quit"], "the takeover handshake must still work"
    other.close()
