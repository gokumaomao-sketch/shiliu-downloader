"""A stream download must report its size, speed and ETA while it runs.

``job.downloaded`` was only ever set once, from the finished file, after
assembly. During the download it stayed 0, so for the whole life of a stream:

  * the Size column read "Unknown" (gui/app.py:789-792),
  * ``avg_speed_bps`` returned 0.0 because it early-returns on ``downloaded <= 0``
    (models.py:118), rendering "—",
  * and the stream ETA computed ``avg = downloaded / done`` = 0, so the estimate
    of the remaining bytes was 0 (models.py:145).

The segment-based progress bar worked, which is why this looked cosmetic — but
every byte-based readout was dead.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from magic_downloader.media.media_engine import MediaDownloadEngine, _Seg, _Track
from magic_downloader.models import DownloadStatus


SEG = b"x" * 4096


def _engine(job, tmp_path):
    eng = MediaDownloadEngine.__new__(MediaDownloadEngine)
    eng.job = job
    eng._lock = threading.RLock()
    eng._seg_done = 0
    eng._seg_total = 0
    eng.max_workers = 2
    eng.rate_limiter = None
    eng._cancelled = lambda: False
    eng._wait_if_paused = lambda: True
    eng._emit = lambda: None
    eng._update_speed = lambda n: None
    eng._set_progress_meta = lambda: None
    eng._concat_target = lambda track, d: d / f"track{track.ext}"
    eng._fetch_segment = lambda seg: SEG
    eng._fetch_bytes = lambda url: b"INIT" * 4
    return eng


def _track(count=3, kind="video", init=None):
    t = _Track(kind=kind, is_fmp4=bool(init), init_url=init or "")
    t.segments = [_Seg(index=i, url=f"https://cdn.test/{i}.ts") for i in range(count)]
    return t


@pytest.fixture
def job(make_job, tmp_path):
    j = make_job(filename="stream.mp4")
    j.save_path = str(tmp_path / "stream.mp4")
    j.media_type = "hls"
    j.media_meta = {}
    j.status = DownloadStatus.DOWNLOADING
    j.downloaded = 0
    return j


def test_bytes_are_counted_as_segments_arrive(job, tmp_path):
    eng = _engine(job, tmp_path)

    eng._download_track(_track(3), tmp_path)

    assert job.downloaded == 3 * len(SEG)


def test_the_size_column_stops_saying_unknown(job, tmp_path):
    """What the user actually sees (gui/app.py:789-792)."""
    from magic_downloader.gui.app import format_bytes

    eng = _engine(job, tmp_path)
    eng._download_track(_track(3), tmp_path)

    rendered = format_bytes(job.downloaded) if job.downloaded else "Unknown"
    assert rendered != "Unknown"
    assert rendered.strip() not in ("0 B", "0.0 B")


def test_average_speed_becomes_reportable(job, tmp_path):
    eng = _engine(job, tmp_path)
    eng._download_track(_track(3), tmp_path)
    job.active_seconds = 2.0

    assert job.avg_speed_bps > 0, "avg_speed_bps early-returns while downloaded is 0"


def test_the_stream_eta_can_be_computed(job, tmp_path):
    eng = _engine(job, tmp_path)
    eng._download_track(_track(3), tmp_path)
    job.media_meta["seg_total"] = 10
    job.media_meta["seg_done"] = 3
    job.speed_bps = 1000.0

    eta = job.eta_seconds
    assert eta is not None and eta > 0, "the ETA divides by the average segment size"


def test_both_tracks_add_up(job, tmp_path):
    """Video + audio are downloaded separately into one job."""
    eng = _engine(job, tmp_path)

    eng._download_track(_track(3, "video"), tmp_path)
    eng._download_track(_track(2, "audio"), tmp_path)

    assert job.downloaded == 5 * len(SEG)


def test_the_fmp4_init_segment_counts_too(job, tmp_path):
    eng = _engine(job, tmp_path)

    eng._download_track(_track(2, init="https://cdn.test/init.mp4"), tmp_path)

    assert job.downloaded == 2 * len(SEG) + len(b"INIT" * 4)


# ── resume ───────────────────────────────────────────────────────────────

def test_a_resume_counts_the_segments_already_on_disk(job, tmp_path):
    """Otherwise a resumed stream reports only what this run happened to fetch."""
    eng = _engine(job, tmp_path)
    eng._download_track(_track(3), tmp_path)          # first run: all three
    first_total = job.downloaded

    eng2 = _engine(job, tmp_path)                     # fresh engine, same folder
    job.downloaded = 0                                # what run() resets it to
    eng2._download_track(_track(3), tmp_path)

    assert job.downloaded == first_total
    assert eng2._seg_done == 3, "already-present segments still count as done"


def test_a_resume_does_not_double_count(job, tmp_path):
    """run() zeroes the counter before the tracks re-seed it from disk."""
    eng = _engine(job, tmp_path)
    eng._download_track(_track(3), tmp_path)
    expected = job.downloaded

    # Simulate exactly what run() does on the next attempt.
    job.downloaded = 0
    eng2 = _engine(job, tmp_path)
    eng2._download_track(_track(3), tmp_path)

    assert job.downloaded == expected, "seeding on top of the persisted value doubles it"


def test_run_resets_the_counter_before_seeding(job, tmp_path, monkeypatch):
    """The reset must actually be wired into run(), not just assumed."""
    eng = _engine(job, tmp_path)
    job.downloaded = 999_999                     # a value persisted by a past run
    seen: list[int] = []

    eng._plan = lambda: [_track(2)]
    eng._tmp_dir = lambda: tmp_path
    eng._sync_plan = lambda d, t: None
    eng._download_track = lambda track, d: seen.append(job.downloaded)
    eng._assemble = lambda tracks: None
    eng._cleanup = lambda d: None
    eng.pause_event = threading.Event()
    eng.pause_event.set()
    job.started_at = None

    eng.run()

    assert seen and seen[0] == 0, "the stale persisted byte count was carried into the run"


def test_a_partial_resume_counts_only_what_exists(job, tmp_path):
    eng = _engine(job, tmp_path)
    track_dir = tmp_path / "video"
    track_dir.mkdir(parents=True, exist_ok=True)
    (track_dir / "000000.seg").write_bytes(SEG)       # one segment already there

    eng._download_track(_track(3), tmp_path)

    assert job.downloaded == 3 * len(SEG)
    assert eng._seg_done == 3
