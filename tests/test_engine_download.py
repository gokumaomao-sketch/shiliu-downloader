"""Download mechanics in ``magic_downloader.engine``.

Covers the speed meter, outgoing request headers, the range/resume decisions,
segment splitting, part-file handling and the pause/cancel flags. Every test
drives the real engine code against a fake origin server; an autouse tripwire
fails the test if anything tries to open a real socket.
"""

from __future__ import annotations

import re
import threading
import time as real_time
from pathlib import Path

import pytest
import requests
import requests.adapters

from magic_downloader.engine import DownloadEngine
from magic_downloader.models import DownloadStatus, SegmentState

UA = "MagicDownloader/test"
OCTET = "application/octet-stream"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Any real HTTP traffic is a test bug, not a download."""

    def _boom(*args, **kwargs):  # pragma: no cover - only runs on failure
        raise AssertionError("a test tried to open a real network connection")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _boom)


class FakeClock:
    """Deterministic stand-in for the ``time`` module inside engine.py."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = float(start)

    def monotonic(self) -> float:
        return self._now

    def time(self) -> float:
        return 1_700_000_000.0 + self._now

    def sleep(self, seconds: float) -> None:
        self._now += float(seconds)

    def advance(self, seconds: float) -> None:
        self._now += float(seconds)


class ScriptedSession:
    """Answers each request with the next scripted response (or raises it)."""

    def __init__(self, *responses) -> None:
        self._queue = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def _serve(self, method: str, url: str, kw: dict):
        self.calls.append((method, url, kw))
        if not self._queue:
            raise AssertionError(f"unscripted {method} {url}")
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def head(self, url, **kw):
        return self._serve("head", url, kw)

    def get(self, url, **kw):
        return self._serve("get", url, kw)

    def sent_ranges(self) -> list[str]:
        return [
            (kw.get("headers") or {}).get("Range")
            for _m, _u, kw in self.calls
            if (kw.get("headers") or {}).get("Range")
        ]


class RangeServerSession:
    """A fake origin server that serves byte ranges of ``body``."""

    def __init__(
        self,
        response_cls,
        body: bytes,
        accept_ranges: str = "bytes",
        honour_range: bool = True,
        content_type: str = OCTET,
    ) -> None:
        self._response_cls = response_cls
        self.body = body
        self.accept_ranges = accept_ranges
        self.honour_range = honour_range
        self.content_type = content_type
        self.ranges: list[str] = []
        self.calls: list[tuple[str, str, dict]] = []

    def _base_headers(self) -> dict[str, str]:
        h = {"Content-Type": self.content_type, "Content-Length": str(len(self.body))}
        if self.accept_ranges:
            h["Accept-Ranges"] = self.accept_ranges
        return h

    def head(self, url, **kw):
        self.calls.append(("head", url, kw))
        return self._response_cls(url=url, headers=self._base_headers(), status_code=200, body=b"")

    def get(self, url, headers=None, **kw):
        self.calls.append(("get", url, dict(kw, headers=headers)))
        rng = (headers or {}).get("Range")
        if rng:
            self.ranges.append(rng)
        if rng and self.honour_range:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else len(self.body) - 1
            chunk = self.body[start : end + 1]
            h = self._base_headers()
            h["Content-Length"] = str(len(chunk))
            h["Content-Range"] = f"bytes {start}-{start + len(chunk) - 1}/{len(self.body)}"
            return self._response_cls(url=url, headers=h, status_code=206, body=chunk)
        return self._response_cls(
            url=url, headers=self._base_headers(), status_code=200, body=self.body
        )


class PatchySession:
    """Plays a script for the first GETs, then delegates to a real fake server."""

    def __init__(self, inner: RangeServerSession, script) -> None:
        self.inner = inner
        self.script = list(script)

    @property
    def ranges(self) -> list[str]:
        return self.inner.ranges

    def head(self, url, **kw):
        return self.inner.head(url, **kw)

    def get(self, url, headers=None, **kw):
        if self.script:
            item = self.script.pop(0)
            rng = (headers or {}).get("Range")
            if rng:
                self.inner.ranges.append(rng)
            if isinstance(item, Exception):
                raise item
            return item
        return self.inner.get(url, headers=headers, **kw)


class DropsMidStream:
    """A 206 whose connection dies after delivering ``deliver`` bytes."""

    def __init__(self, url: str, body: bytes, deliver: int, total: int) -> None:
        self.url = url
        self.status_code = 206
        self.headers = {
            "Content-Length": str(len(body)),
            "Content-Range": f"bytes 0-{len(body) - 1}/{total}",
        }
        self._body = body
        self._deliver = deliver

    def iter_content(self, chunk_size=1):
        yield self._body[: self._deliver]
        raise requests.ConnectionError("connection reset by peer")

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_engine(job, session=None, **kw) -> DownloadEngine:
    eng = DownloadEngine(job, UA, **kw)
    if session is not None:
        eng._session = session
    return eng


@pytest.fixture
def range_server(fake_response):
    def _make(body: bytes, **kw) -> RangeServerSession:
        return RangeServerSession(fake_response, body, **kw)

    return _make


# --------------------------------------------------------------------------
# _update_speed — the rolling speed window
# --------------------------------------------------------------------------


def test_speed_matches_a_steady_transfer_rate(make_job, monkeypatch):
    """8 chunks of 1000 B per second must read back as roughly 8 KB/s."""
    import magic_downloader.engine as engine_mod

    clock = FakeClock()
    monkeypatch.setattr(engine_mod, "time", clock)
    eng = make_engine(make_job())

    for _ in range(80):  # 10 s of transfer, 0.125 s apart (exact in binary)
        clock.advance(0.125)
        eng._update_speed(1000)

    # Window holds t-3.0 .. t inclusive: 25 samples over a 3 s span.
    assert len(eng._speed_window) == 25
    assert eng._speed_total == 25_000
    assert eng.job.speed_bps == pytest.approx(25_000 / 3.0)
    assert eng.job.speed_bps == pytest.approx(8000, rel=0.05)


def test_samples_older_than_the_window_stop_counting(make_job, monkeypatch):
    import magic_downloader.engine as engine_mod

    clock = FakeClock()
    monkeypatch.setattr(engine_mod, "time", clock)
    eng = make_engine(make_job())

    eng._update_speed(10_000_000)  # a huge burst...
    clock.advance(3.5)  # ...that finished long ago
    eng._update_speed(1000)

    assert len(eng._speed_window) == 1
    assert eng._speed_total == 1000, "the expired burst must be subtracted from the total"


def test_recent_samples_stay_in_the_window(make_job, monkeypatch):
    import magic_downloader.engine as engine_mod

    clock = FakeClock()
    monkeypatch.setattr(engine_mod, "time", clock)
    eng = make_engine(make_job())

    eng._update_speed(4000)
    clock.advance(2.5)  # still inside the ~3 s window
    eng._update_speed(2000)

    assert len(eng._speed_window) == 2
    assert eng._speed_total == 6000
    assert eng.job.speed_bps == pytest.approx(6000 / 2.5)


def test_window_stays_bounded_over_a_long_download(make_job, monkeypatch):
    """Regression: the window used to be rebuilt and re-summed on every chunk."""
    import magic_downloader.engine as engine_mod

    clock = FakeClock()
    monkeypatch.setattr(engine_mod, "time", clock)
    eng = make_engine(make_job())

    for _ in range(2000):  # 64 chunks/s for ~31 s
        clock.advance(0.015625)
        eng._update_speed(512)

    assert len(eng._speed_window) <= 200, "window must not grow with download length"
    assert eng._speed_total == sum(n for _t, n in eng._speed_window)
    assert eng.job.speed_bps == pytest.approx(512 * 64, rel=0.05)


def test_running_total_survives_concurrent_connections(make_job, monkeypatch):
    """Eight segments hammering the meter must not lose bytes to a race."""
    import magic_downloader.engine as engine_mod

    clock = FakeClock()  # frozen: nothing can expire during the test
    monkeypatch.setattr(engine_mod, "time", clock)
    eng = make_engine(make_job())

    def worker():
        for _ in range(200):
            eng._update_speed(100)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert eng._speed_total == 8 * 200 * 100
    assert len(eng._speed_window) == 8 * 200


def test_update_speed_feeds_the_active_time_clock(make_job):
    """The Avg-speed column is driven from here, not from wall-clock elapsed."""
    eng = make_engine(make_job())

    eng._update_speed(10)
    real_time.sleep(0.05)
    eng._update_speed(10)

    assert 0.02 <= eng.job.active_seconds <= 1.0


# --------------------------------------------------------------------------
# _headers()
# --------------------------------------------------------------------------


def test_headers_are_minimal_when_the_job_has_no_browser_context(make_job):
    h = make_engine(make_job())._headers()

    assert h == {"User-Agent": UA}


def test_headers_carry_cookie_referer_and_extras(make_job):
    job = make_job(
        cookie="sid=abc",
        referrer="https://example.test/page",
        extra_headers={"X-Token": "42"},
    )

    h = make_engine(job)._headers()

    assert h["Cookie"] == "sid=abc"
    assert h["Referer"] == "https://example.test/page"
    assert h["X-Token"] == "42"


def test_extra_headers_ignore_blank_names_and_values(make_job):
    job = make_job(extra_headers={"": "x", "X-Empty": "", "X-Good": "1"})

    h = make_engine(job)._headers()

    assert "" not in h
    assert "X-Empty" not in h
    assert h["X-Good"] == "1"


def test_range_extra_is_merged_and_wins_over_defaults(make_job):
    job = make_job(cookie="sid=abc", extra_headers={"X-Token": "42"})

    h = make_engine(job)._headers({"Range": "bytes=100-199", "User-Agent": "override"})

    assert h["Range"] == "bytes=100-199"
    assert h["User-Agent"] == "override", "an explicit extra must win"
    assert h["Cookie"] == "sid=abc" and h["X-Token"] == "42"


def test_session_headers_are_seeded_from_the_job(make_job):
    plain = make_engine(make_job())
    rich = make_engine(make_job(cookie="sid=1", referrer="https://r.test/", extra_headers={"A": "b"}))

    assert "Cookie" not in plain._session.headers
    assert "Referer" not in plain._session.headers
    assert rich._session.headers["User-Agent"] == UA
    assert rich._session.headers["Cookie"] == "sid=1"
    assert rich._session.headers["Referer"] == "https://r.test/"
    assert rich._session.headers["A"] == "b"


# --------------------------------------------------------------------------
# probe() — size, name and the range-support decision
# --------------------------------------------------------------------------


def _head(fake_response, **headers):
    hdrs = {"Content-Type": OCTET}
    hdrs.update(headers)
    return fake_response(url="https://example.test/file.bin", headers=hdrs, status_code=200)


def test_probe_reads_size_etag_and_final_url(make_job, fake_response, tmp_path):
    head = fake_response(
        url="https://cdn.example.test/real.bin",
        headers={
            "Content-Type": OCTET,
            "Content-Length": "5000",
            "Accept-Ranges": "bytes",
            "ETag": '"deadbeef"',
            "Last-Modified": "Tue, 01 Jul 2025 10:00:00 GMT",
        },
    )
    verify = fake_response(headers={"Content-Range": "bytes 0-0/5000"}, status_code=206)
    session = ScriptedSession(head, verify)
    job = make_job(save_path=str(tmp_path / "file.bin"))
    seen: list[str] = []
    eng = make_engine(job, session, on_progress=lambda j: seen.append(j.status.value))

    eng.probe()

    assert job.url == "https://cdn.example.test/real.bin", "redirects must be followed"
    assert job.total_size == 5000
    assert job.etag == '"deadbeef"'
    assert job.last_modified == "Tue, 01 Jul 2025 10:00:00 GMT"
    assert job.supports_ranges is True
    assert DownloadStatus.CONNECTING.value in seen


def test_probe_sends_a_one_byte_range_check(make_job, fake_response, tmp_path):
    session = ScriptedSession(
        _head(fake_response, **{"Content-Length": "5000", "Accept-Ranges": "bytes"}),
        fake_response(status_code=206),
    )
    job = make_job(save_path=str(tmp_path / "file.bin"), cookie="sid=9")

    make_engine(job, session).probe()

    assert session.sent_ranges() == ["bytes=0-0"]
    range_call = session.calls[-1][2]["headers"]
    assert range_call["Cookie"] == "sid=9", "the probe must reuse the job's auth headers"


def test_accept_ranges_none_plus_ignored_range_disables_ranges(make_job, fake_response, tmp_path):
    session = ScriptedSession(
        _head(fake_response, **{"Content-Length": "5000", "Accept-Ranges": "none"}),
        fake_response(status_code=200),
    )
    job = make_job(save_path=str(tmp_path / "file.bin"))

    make_engine(job, session).probe()

    assert job.supports_ranges is False


def test_missing_accept_ranges_with_a_200_range_reply_disables_ranges(
    make_job, fake_response, tmp_path
):
    """No advertisement and the Range was ignored -> single stream."""
    session = ScriptedSession(
        _head(fake_response, **{"Content-Length": "5000"}),
        fake_response(status_code=200),
    )
    job = make_job(save_path=str(tmp_path / "file.bin"))

    make_engine(job, session).probe()

    assert job.supports_ranges is False


def test_range_support_confirmed_by_206_even_without_accept_ranges(
    make_job, fake_response, tmp_path
):
    session = ScriptedSession(
        _head(fake_response, **{"Content-Length": "5000", "Accept-Ranges": "none"}),
        fake_response(status_code=206),
    )
    job = make_job(save_path=str(tmp_path / "file.bin"))

    make_engine(job, session).probe()

    assert job.supports_ranges is True, "an actual 206 outranks the advertisement"


def test_probe_falls_back_to_get_when_head_is_useless(make_job, fake_response, tmp_path):
    session = ScriptedSession(
        fake_response(status_code=405, headers={"Content-Type": "text/plain"}),
        _head(fake_response, **{"Content-Length": "1234", "Accept-Ranges": "bytes"}),
        fake_response(status_code=206),
    )
    job = make_job(save_path=str(tmp_path / "file.bin"))

    make_engine(job, session).probe()

    assert [c[0] for c in session.calls][:2] == ["head", "get"]
    assert job.total_size == 1234


def test_probe_accepts_range_only_media_cdn(make_job, fake_response, tmp_path):
    session = ScriptedSession(
        fake_response(status_code=403, headers={"Content-Type": "text/html", "Content-Length": "170"}),
        fake_response(status_code=206, headers={
            "Content-Type": OCTET,
            "Content-Length": "1",
            "Content-Range": "bytes 0-0/21370131",
        }),
    )
    job = make_job(save_path=str(tmp_path / "clip.mp4"))

    make_engine(job, session).probe()

    assert job.total_size == 21370131
    assert job.supports_ranges is True
    assert session.sent_ranges() == ["bytes=0-0"]


def test_probe_falls_back_to_get_when_head_raises(make_job, fake_response, tmp_path):
    session = ScriptedSession(
        requests.ConnectionError("HEAD refused"),
        _head(fake_response, **{"Content-Length": "77", "Accept-Ranges": "bytes"}),
        fake_response(status_code=206),
    )
    job = make_job(save_path=str(tmp_path / "file.bin"))

    make_engine(job, session).probe()

    assert job.total_size == 77


def test_probe_skips_the_range_check_for_tiny_files(make_job, fake_response, tmp_path):
    session = ScriptedSession(_head(fake_response, **{"Content-Length": "1"}))
    job = make_job(save_path=str(tmp_path / "file.bin"))

    make_engine(job, session).probe()

    assert len(session.calls) == 1, "a 1-byte file needs no range verification"


def test_probe_rejects_an_html_sign_in_page(make_job, fake_response, tmp_path):
    session = ScriptedSession(
        fake_response(
            url="https://example.test/download",
            headers={"Content-Type": "text/html; charset=utf-8", "Content-Length": "4096"},
        )
    )
    job = make_job(save_path=str(tmp_path / "file.bin"))

    with pytest.raises(RuntimeError, match="网页"):
        make_engine(job, session).probe()


def test_probe_allows_html_when_the_server_says_attachment(make_job, fake_response, tmp_path):
    session = ScriptedSession(
        fake_response(
            url="https://example.test/export",
            headers={
                "Content-Type": "text/html",
                "Content-Length": "4096",
                "Content-Disposition": 'attachment; filename="report.html"',
            },
        ),
        fake_response(status_code=206),
    )
    job = make_job(filename="report.html", save_path=str(tmp_path / "report.html"))

    make_engine(job, session).probe()  # must not raise

    assert job.total_size == 4096


def test_probe_renames_a_junk_filename_and_dedupes_the_save_path(
    make_job, fake_response, tmp_path
):
    junk = "a5de8a50-7e22-4163-97a4-914d6644caa7"
    (tmp_path / "report.pdf").write_bytes(b"an older download")
    session = ScriptedSession(
        fake_response(
            url="https://example.test/dl/" + junk,
            headers={
                "Content-Type": OCTET,
                "Content-Length": "5000",
                "Content-Disposition": 'attachment; filename="report.pdf"',
                "Accept-Ranges": "bytes",
            },
        ),
        fake_response(status_code=206),
    )
    job = make_job(filename=junk, save_path=str(tmp_path / junk))

    make_engine(job, session).probe()

    assert job.filename == "report.pdf"
    assert Path(job.save_path) == tmp_path / "report (1).pdf", "must not overwrite an existing file"


# Was xfail(strict) while the bug was live: a 200 answer to the range probe was
# only believed when nothing had been advertised, so a server claiming
# "Accept-Ranges: bytes" and then ignoring Range kept supports_ranges=True.
# What the server DOES now beats what it advertises.
def test_probe_should_disbelieve_accept_ranges_when_range_is_ignored(
    make_job, fake_response, tmp_path
):
    session = ScriptedSession(
        _head(fake_response, **{"Content-Length": "5000", "Accept-Ranges": "bytes"}),
        fake_response(status_code=200),  # Range ignored: full body, not a slice
    )
    job = make_job(save_path=str(tmp_path / "file.bin"))

    make_engine(job, session).probe()

    assert job.supports_ranges is False


# --------------------------------------------------------------------------
# segment splitting and the .part file
# --------------------------------------------------------------------------


def test_small_files_use_a_single_segment(make_job):
    job = make_job(total_size=1_500_000, connections=8)

    segments = make_engine(job)._build_segments()

    assert len(segments) == 1
    assert (segments[0].start, segments[0].end) == (0, 1_499_999)


def test_medium_files_are_capped_at_four_segments(make_job):
    job = make_job(total_size=5 * 1024 * 1024, connections=16)

    segments = make_engine(job)._build_segments()

    assert len(segments) == 4


def test_segments_cover_the_whole_file_and_cap_at_32(make_job):
    total = 100 * 1024 * 1024
    job = make_job(total_size=total, connections=64)

    segments = make_engine(job)._build_segments()

    assert len(segments) == 32, "connections are capped at 32"
    assert segments[0].start == 0
    assert segments[-1].end == total - 1
    for prev, nxt in zip(segments, segments[1:]):
        assert nxt.start == prev.end + 1, "segments must be contiguous with no gaps"
    assert sum(s.end - s.start + 1 for s in segments) == total


def test_no_segments_without_a_known_size(make_job):
    assert make_engine(make_job(total_size=0))._build_segments() == []


def test_segments_with_progress_are_reused(make_job):
    existing = [
        SegmentState(index=0, start=0, end=999, downloaded=400),
        SegmentState(index=1, start=1000, end=1999, downloaded=0),
    ]
    job = make_job(total_size=2000, connections=8, segments=existing)

    segments = make_engine(job)._build_segments()

    assert segments is existing, "a resumed job must keep its partial segments"


def test_part_file_is_preallocated_to_the_full_size(make_job, tmp_path):
    job = make_job(save_path=str(tmp_path / "f.bin"))

    part = make_engine(job)._ensure_part_file(4096)

    assert part == tmp_path / "f.bin.part"
    assert part.stat().st_size == 4096


def test_part_file_with_the_right_size_is_kept_for_resume(make_job, tmp_path):
    part = tmp_path / "f.bin.part"
    part.write_bytes(b"A" * 500 + b"\0" * 3596)
    job = make_job(save_path=str(tmp_path / "f.bin"))

    make_engine(job)._ensure_part_file(4096)

    assert part.read_bytes()[:500] == b"A" * 500, "resume data must not be wiped"


def test_part_file_of_the_wrong_size_is_reallocated(make_job, tmp_path):
    part = tmp_path / "f.bin.part"
    part.write_bytes(b"A" * 10)
    job = make_job(save_path=str(tmp_path / "f.bin"))

    make_engine(job)._ensure_part_file(4096)

    assert part.stat().st_size == 4096
    assert part.read_bytes()[:10] == b"\0" * 10


# --------------------------------------------------------------------------
# multi-segment transfers
# --------------------------------------------------------------------------


def test_multipart_download_reassembles_the_file(make_job, range_server, tmp_path):
    body = bytes(range(256)) * 8200  # ~2.1 MB -> 4 segments
    server = range_server(body)
    job = make_job(save_path=str(tmp_path / "big.bin"), connections=4)
    eng = make_engine(job, server, chunk_size=64 * 1024)

    eng.run()

    assert job.status == DownloadStatus.COMPLETE
    assert len(job.segments) == 4
    assert len([r for r in server.ranges if r != "bytes=0-0"]) == 4
    assert (tmp_path / "big.bin").read_bytes() == body
    assert not (tmp_path / "big.bin.part").exists(), "the .part file must be renamed away"
    assert job.downloaded == len(body)


def test_multipart_resume_only_requests_the_missing_ranges(make_job, range_server, tmp_path):
    body = bytes(range(256)) * 16  # 4096 bytes
    part = tmp_path / "r.bin.part"
    part.write_bytes(body[:500] + b"\0" * (len(body) - 500))
    job = make_job(
        save_path=str(tmp_path / "r.bin"),
        total_size=len(body),
        downloaded=500,
        connections=4,
        segments=[
            SegmentState(index=0, start=0, end=1023, downloaded=500),
            SegmentState(index=1, start=1024, end=2047, downloaded=0),
            SegmentState(index=2, start=2048, end=3071, downloaded=0),
            SegmentState(index=3, start=3072, end=4095, downloaded=0),
        ],
    )
    server = range_server(body)
    eng = make_engine(job, server, chunk_size=256)

    eng._run_multipart()

    assert sorted(server.ranges) == sorted(
        ["bytes=500-1023", "bytes=1024-2047", "bytes=2048-3071", "bytes=3072-4095"]
    )
    assert part.read_bytes() == body, "already-downloaded bytes must be kept and not refetched"
    assert job.downloaded == len(body)


def test_segment_retries_once_after_a_dropped_connection(
    make_job, range_server, tmp_path, monkeypatch
):
    import magic_downloader.engine as engine_mod

    monkeypatch.setattr(engine_mod, "time", FakeClock())  # the 1 s retry back-off
    body = bytes(range(256)) * 8
    server = PatchySession(range_server(body), [requests.ConnectionError("reset")])
    job = make_job(save_path=str(tmp_path / "s.bin"), total_size=len(body))
    eng = make_engine(job, server, chunk_size=256)
    part = eng._ensure_part_file(len(body))
    seg = SegmentState(index=0, start=0, end=len(body) - 1, downloaded=0)

    eng._download_segment(part, seg)

    assert seg.downloaded == len(body)
    assert part.read_bytes() == body


def test_segment_failure_after_the_retry_is_reported(
    make_job, range_server, tmp_path, monkeypatch
):
    import magic_downloader.engine as engine_mod

    monkeypatch.setattr(engine_mod, "time", FakeClock())
    body = b"x" * 1024
    server = PatchySession(
        range_server(body),
        [requests.ConnectionError("reset") for _ in range(4)],
    )
    job = make_job(save_path=str(tmp_path / "s.bin"), total_size=len(body))
    eng = make_engine(job, server, chunk_size=256)
    part = eng._ensure_part_file(len(body))

    with pytest.raises(RuntimeError, match="Segment 0 failed"):
        eng._download_segment(part, SegmentState(index=0, start=0, end=len(body) - 1))


# Was xfail(strict) while the bug was live: the retry reused the Range header
# built before the drop (from the original offset) but seeked to
# start+downloaded, duplicating the bytes already written at the wrong offset.
# Fixed by rebuilding the Range from seg.start + seg.downloaded — this now passes
# and must keep passing.
def test_retry_after_a_partial_segment_must_not_duplicate_bytes(
    make_job, range_server, tmp_path, monkeypatch
):
    import magic_downloader.engine as engine_mod

    monkeypatch.setattr(engine_mod, "time", FakeClock())
    body = bytes(range(256)) * 8  # 2048 bytes
    dropped = DropsMidStream("https://example.test/f", body, deliver=500, total=len(body))
    server = PatchySession(range_server(body), [dropped])
    job = make_job(save_path=str(tmp_path / "s.bin"), total_size=len(body))
    eng = make_engine(job, server, chunk_size=512)
    part = eng._ensure_part_file(len(body))
    seg = SegmentState(index=0, start=0, end=len(body) - 1)

    eng._download_segment(part, seg)

    assert server.ranges == ["bytes=0-2047", "bytes=500-2047"], "the retry must resume, not restart"
    assert seg.downloaded == len(body), "re-fetched bytes would inflate the segment counter"
    assert part.stat().st_size == len(body), "the segment must not overrun its slot"
    assert part.read_bytes() == body


# Was xfail(strict) while the bug was live: every segment fetched the whole body
# and wrote it at its own offset, giving a corrupt oversized file reported as
# Complete. probe() now detects the ignored range and the job runs single-stream.
def test_multipart_against_a_range_ignoring_server_keeps_the_file_intact(
    make_job, range_server, tmp_path
):
    body = bytes(range(256)) * 8200  # ~2.1 MB -> 4 segments
    server = range_server(body, accept_ranges="bytes", honour_range=False)
    job = make_job(save_path=str(tmp_path / "big.bin"), connections=4)
    eng = make_engine(job, server, chunk_size=256 * 1024)

    eng.run()

    assert (tmp_path / "big.bin").read_bytes() == body


# --------------------------------------------------------------------------
# single-stream transfers and resume
# --------------------------------------------------------------------------


def test_single_stream_resume_appends_with_a_range_header(make_job, range_server, tmp_path):
    body = bytes(range(256)) * 8
    part = tmp_path / "one.bin.part"
    part.write_bytes(body[:600])
    server = range_server(body)
    job = make_job(
        save_path=str(tmp_path / "one.bin"),
        total_size=len(body),
        downloaded=600,
        supports_ranges=True,
    )
    eng = make_engine(job, server, chunk_size=256)

    eng._run_single()

    assert server.ranges == ["bytes=600-"]
    assert part.read_bytes() == body
    assert job.downloaded == len(body)


def test_resume_restarts_when_the_server_ignores_the_range(make_job, range_server, tmp_path):
    """A 200 answer to a resume request means start over, not append."""
    body = bytes(range(256)) * 8
    part = tmp_path / "one.bin.part"
    part.write_bytes(body[:600])
    server = range_server(body, honour_range=False)
    job = make_job(save_path=str(tmp_path / "one.bin"), total_size=len(body), downloaded=600)
    eng = make_engine(job, server, chunk_size=256)

    eng._run_single()

    assert part.read_bytes() == body, "the partial prefix must be discarded, not appended to"
    assert job.downloaded == len(body)


def test_resume_learns_the_total_size_from_content_range(make_job, range_server, tmp_path):
    body = bytes(range(256)) * 8
    part = tmp_path / "u.bin.part"
    part.write_bytes(body[:500])
    server = range_server(body)
    job = make_job(save_path=str(tmp_path / "u.bin"), total_size=0, downloaded=500)
    eng = make_engine(job, server, chunk_size=256)

    eng._run_single()

    assert job.total_size == len(body), "size comes from Content-Range on a 206"
    assert part.read_bytes() == body


def test_unknown_size_is_taken_from_content_length_on_a_200(make_job, range_server, tmp_path):
    body = b"abc" * 300
    server = range_server(body, accept_ranges="")
    job = make_job(save_path=str(tmp_path / "u.bin"), total_size=0)
    eng = make_engine(job, server, chunk_size=128)

    eng._run_single()

    assert job.total_size == len(body)
    assert (tmp_path / "u.bin.part").read_bytes() == body


def test_rate_limiter_sees_every_byte(make_job, range_server, tmp_path):
    body = b"z" * 1000
    seen: list[int] = []

    class Limiter:
        def throttle(self, n):
            seen.append(n)

    job = make_job(save_path=str(tmp_path / "l.bin"), total_size=len(body))
    eng = make_engine(job, range_server(body), chunk_size=256, rate_limiter=Limiter())

    eng._run_single()

    assert sum(seen) == len(body)
    assert len(seen) == 4, "one throttle call per chunk"


# --------------------------------------------------------------------------
# _finalize
# --------------------------------------------------------------------------


def test_finalize_renames_the_part_file(make_job, tmp_path):
    part = tmp_path / "done.bin.part"
    part.write_bytes(b"payload")
    job = make_job(save_path=str(tmp_path / "done.bin"), total_size=7, downloaded=7, error="boom")

    make_engine(job)._finalize()

    assert not part.exists()
    assert (tmp_path / "done.bin").read_bytes() == b"payload"
    assert job.status == DownloadStatus.COMPLETE
    assert job.error == ""
    assert job.finished_at is not None


def test_finalize_replaces_an_existing_file(make_job, tmp_path):
    (tmp_path / "dup.bin").write_bytes(b"stale")
    (tmp_path / "dup.bin.part").write_bytes(b"fresh")
    job = make_job(save_path=str(tmp_path / "dup.bin"), total_size=5, downloaded=5)

    make_engine(job)._finalize()

    assert (tmp_path / "dup.bin").read_bytes() == b"fresh"


def test_finalize_learns_the_size_of_an_unknown_length_download(make_job, tmp_path):
    (tmp_path / "x.bin.part").write_bytes(b"0123456789")
    job = make_job(save_path=str(tmp_path / "x.bin"), total_size=0, downloaded=0)

    make_engine(job)._finalize()

    assert job.total_size == 10
    assert job.downloaded == 10


# --------------------------------------------------------------------------
# pause / cancel
# --------------------------------------------------------------------------


def test_wait_is_a_no_op_while_running(make_job):
    assert make_engine(make_job())._wait_if_paused() is True


def test_wait_reports_a_stop(make_job):
    eng = make_engine(make_job())
    eng.stop()

    assert eng._wait_if_paused() is False


def test_wait_reports_an_external_cancel(make_job):
    cancelled = {"v": False}
    eng = make_engine(make_job(), cancel_check=lambda: cancelled["v"])
    assert eng._wait_if_paused() is True

    cancelled["v"] = True

    assert eng._wait_if_paused() is False


def test_a_paused_transfer_waits_and_then_continues(make_job):
    eng = make_engine(make_job())
    eng.pause()
    threading.Timer(0.3, eng.resume).start()

    started = real_time.monotonic()
    result = eng._wait_if_paused()

    assert result is True
    assert real_time.monotonic() - started >= 0.25, "it must actually have waited"


def test_stopping_a_paused_transfer_unblocks_it(make_job):
    eng = make_engine(make_job())
    eng.pause()
    threading.Timer(0.2, eng.stop).start()

    assert eng._wait_if_paused() is False


def test_cancel_stops_before_any_bytes_are_written(make_job, range_server, tmp_path):
    body = b"q" * 1000
    job = make_job(save_path=str(tmp_path / "c.bin"), total_size=len(body))
    eng = make_engine(job, range_server(body), chunk_size=100, cancel_check=lambda: True)

    eng._run_single()

    assert job.status == DownloadStatus.CANCELLED
    assert job.downloaded == 0
    assert (tmp_path / "c.bin.part").stat().st_size == 0


def test_pausing_mid_stream_keeps_the_partial_file(make_job, range_server, tmp_path):
    body = b"p" * 1000
    job = make_job(save_path=str(tmp_path / "p.bin"), total_size=len(body))

    def on_progress(j):
        j.status = DownloadStatus.PAUSED

    eng = make_engine(job, range_server(body), chunk_size=250, on_progress=on_progress)

    eng._run_single()

    assert job.status == DownloadStatus.PAUSED
    assert job.downloaded == 250, "the stream must stop at the first chunk after the pause"
    assert (tmp_path / "p.bin.part").read_bytes() == b"p" * 250
    assert not (tmp_path / "p.bin").exists()


def test_run_cancels_without_touching_the_network(make_job, tmp_path):
    session = ScriptedSession()  # any request raises
    job = make_job(
        save_path=str(tmp_path / "n.bin"),
        total_size=1000,
        segments=[SegmentState(index=0, start=0, end=999, downloaded=10)],
    )
    eng = make_engine(job, session, cancel_check=lambda: True)

    eng.run()

    assert job.status == DownloadStatus.CANCELLED
    assert session.calls == []


def test_run_reports_the_failure_message_on_the_job(make_job, fake_response, tmp_path):
    session = ScriptedSession(
        fake_response(
            url="https://example.test/download",
            headers={"Content-Type": "text/html", "Content-Length": "900"},
        )
    )
    job = make_job(save_path=str(tmp_path / "f.bin"))
    emitted: list[DownloadStatus] = []
    eng = make_engine(job, session, on_progress=lambda j: emitted.append(j.status))

    eng.run()

    assert job.status == DownloadStatus.FAILED
    assert "网页" in job.error
    assert job.speed_bps == 0.0
    assert emitted[-1] == DownloadStatus.FAILED
