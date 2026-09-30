"""A dropped connection mid-segment must not corrupt the file.

Regression suite for the worst bug this project has had: the retry after a
network error reused the Range header built for the FIRST attempt (asking the
server for the segment from its original start) while seeking to the UPDATED
write position. The already-received bytes were written a second time at the
wrong offset, seg.downloaded was inflated so the tail was never fetched, and the
job still finished as COMPLETE — a silently corrupt download.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import magic_downloader.engine as engine_mod
from magic_downloader.engine import DownloadEngine
from magic_downloader.models import DownloadStatus


PAYLOAD = bytes((i * 7 + 11) % 251 for i in range(4096))  # deterministic, non-uniform


class _Resp:
    """Serves a byte range, optionally dying part-way through."""

    def __init__(self, body: bytes, status_code: int = 206, die_after: int | None = None):
        self.body = body
        self.status_code = status_code
        self.headers = {}
        self._die_after = die_after

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def iter_content(self, chunk_size=1):
        sent = 0
        for i in range(0, len(self.body), chunk_size):
            chunk = self.body[i : i + chunk_size]
            if self._die_after is not None and sent >= self._die_after:
                raise engine_mod.requests.ConnectionError("connection reset by peer")
            sent += len(chunk)
            yield chunk


class RangeServer:
    """Honours the Range header, and drops the first response mid-stream.

    Records every Range it was asked for so a test can assert the retry asked
    for the *remaining* bytes rather than repeating what it already had.
    """

    def __init__(self, payload: bytes, die_after: int, chunk: int):
        self.payload = payload
        self.die_after = die_after
        self.chunk = chunk
        self.ranges: list[tuple[int, int]] = []
        self._served = 0

    def get(self, url, headers=None, stream=False, timeout=None, **kw):
        rng = (headers or {}).get("Range", "")
        m = re.match(r"bytes=(\d+)-(\d+)", rng)
        assert m, f"segment requests must carry a byte range, got {rng!r}"
        start, end = int(m.group(1)), int(m.group(2))
        self.ranges.append((start, end))
        body = self.payload[start : end + 1]
        self._served += 1
        die = self.die_after if self._served == 1 else None
        return _Resp(body, 206, die_after=die)


def _engine(job, session, chunk=256):
    eng = DownloadEngine.__new__(DownloadEngine)
    eng.job = job
    eng._session = session
    eng.chunk_size = chunk
    eng.timeout = 5
    eng._stop = False
    eng.cancel_check = lambda: False
    eng.rate_limiter = None
    eng.user_agent = "test"
    eng._emit = lambda: None
    eng._account = lambda n: None
    eng._wait_if_paused = lambda: True
    import threading

    eng._lock = threading.RLock()
    eng.pause_event = threading.Event()
    eng.pause_event.set()
    return eng


@pytest.fixture
def part_job(tmp_path, make_job):
    """A job whose .part file is pre-allocated, with one whole-file segment."""
    from magic_downloader.models import SegmentState

    target = tmp_path / "payload.bin"
    job = make_job(filename="payload.bin", url="https://example.test/payload.bin")
    job.save_path = str(target)
    job.total_size = len(PAYLOAD)
    job.supports_ranges = True
    job.status = DownloadStatus.DOWNLOADING
    seg = SegmentState(index=0, start=0, end=len(PAYLOAD) - 1, downloaded=0)
    job.segments = [seg]

    part = Path(str(target) + ".part")
    part.write_bytes(b"\x00" * len(PAYLOAD))   # what _ensure_part_file leaves
    return job, seg, part


def test_retry_after_a_dropped_connection_writes_the_correct_bytes(part_job):
    """The whole point: the file on disk must equal what the server has."""
    job, seg, part = part_job
    server = RangeServer(PAYLOAD, die_after=1024, chunk=256)
    eng = _engine(job, server, chunk=256)

    eng._download_segment(part, seg)

    assert part.read_bytes() == PAYLOAD, "the downloaded file must match the source exactly"


def test_retry_requests_only_the_bytes_still_missing(part_job):
    """The retry must not re-request bytes already written."""
    job, seg, part = part_job
    server = RangeServer(PAYLOAD, die_after=1024, chunk=256)
    eng = _engine(job, server, chunk=256)

    eng._download_segment(part, seg)

    assert len(server.ranges) == 2, "expected an initial request and one retry"
    first_start, _ = server.ranges[0]
    retry_start, _ = server.ranges[1]
    assert first_start == 0
    assert retry_start == 1024, (
        "the retry asked for bytes from "
        f"{retry_start} but the first attempt already stored 1024 — re-requesting "
        "them writes duplicates at the wrong offset"
    )


def test_segment_progress_is_not_inflated_by_the_retry(part_job):
    """Inflated progress is what made the corrupt job report Complete."""
    job, seg, part = part_job
    server = RangeServer(PAYLOAD, die_after=1024, chunk=256)
    eng = _engine(job, server, chunk=256)

    eng._download_segment(part, seg)

    assert seg.downloaded == len(PAYLOAD), "downloaded must equal the segment size"
    assert job.downloaded == len(PAYLOAD)


def test_no_tail_of_zeros_is_left_behind(part_job):
    """The old bug finished early, leaving pre-allocated zeros at the end."""
    job, seg, part = part_job
    server = RangeServer(PAYLOAD, die_after=1024, chunk=256)
    eng = _engine(job, server, chunk=256)

    eng._download_segment(part, seg)

    data = part.read_bytes()
    assert data[-64:] != b"\x00" * 64, "the end of the file was never fetched"
    assert data[-64:] == PAYLOAD[-64:]


def test_clean_download_without_any_drop_still_works(part_job):
    """Guard: the retry path must not disturb the normal case."""
    job, seg, part = part_job
    server = RangeServer(PAYLOAD, die_after=None, chunk=256)
    eng = _engine(job, server, chunk=256)

    eng._download_segment(part, seg)

    assert part.read_bytes() == PAYLOAD
    assert len(server.ranges) == 1, "no retry should have been needed"


def test_resuming_a_partly_downloaded_segment_asks_from_the_right_offset(part_job):
    """A segment resumed from saved state must not refetch what it has."""
    job, seg, part = part_job
    seg.downloaded = 2048                     # pretend a previous run got this far
    part.write_bytes(PAYLOAD[:2048] + b"\x00" * (len(PAYLOAD) - 2048))
    server = RangeServer(PAYLOAD, die_after=None, chunk=256)
    eng = _engine(job, server, chunk=256)

    eng._download_segment(part, seg)

    assert server.ranges[0][0] == 2048, "resume must start where it left off"
    assert part.read_bytes() == PAYLOAD
