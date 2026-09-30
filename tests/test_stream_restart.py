"""Re-downloading or re-picking the quality of a stream must not reuse segments.

Segments live in a scratch folder keyed only by index, and that folder's path is
stable across runs. Nothing cleared it, so:

  * "Re-download" — whose dialog promises "from the start (discarding current
    progress)" — reused every segment on disk and finished instantly with the
    same, possibly broken, file;
  * switching 1080p -> 480p kept the old 1080p segments under the same index
    filenames and fetched only the extra ones, assembling a file that
    interleaved two resolutions.

The wipe happens inside the engine (see MediaDownloadEngine._sync_plan) because
cancel_job signals the download thread without waiting for it — deleting the
folder from the manager could race workers still writing into it.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from magic_downloader.media.media_engine import MediaDownloadEngine, _Seg, _Track
from magic_downloader.models import DownloadStatus


def _engine(job):
    eng = MediaDownloadEngine.__new__(MediaDownloadEngine)
    eng.job = job
    eng._lock = threading.RLock()
    eng._seg_done = 0
    return eng


def _tracks(count=3, kind="video", fmp4=False):
    t = _Track(kind=kind, is_fmp4=fmp4)
    t.segments = [_Seg(index=i, url=f"https://cdn.test/{kind}/{i}.ts") for i in range(count)]
    return [t]


@pytest.fixture
def scratch(tmp_path, make_job):
    """A scratch folder holding segments from a previous 1080p run."""
    job = make_job(filename="movie.mp4")
    job.save_path = str(tmp_path / "movie.mp4")
    job.media_meta = {"height": 1080}
    d = tmp_path / "scratch"
    (d / "video").mkdir(parents=True)
    for i in range(3):
        (d / "video" / f"{i:06d}.seg").write_bytes(b"OLD-1080p")
    return job, d


def _segments_on_disk(d: Path):
    return sorted(p.name for p in (d / "video").glob("*.seg")) if (d / "video").exists() else []


def test_an_explicit_restart_discards_the_old_segments(scratch):
    job, d = scratch
    job.media_meta["restart_stream"] = True          # what redownload_job sets
    eng = _engine(job)

    eng._sync_plan(d, _tracks())

    assert _segments_on_disk(d) == [], "Re-download must actually start from scratch"


def test_a_restart_clears_the_progress_counters(scratch):
    job, d = scratch
    job.media_meta["restart_stream"] = True
    eng = _engine(job)
    eng._seg_done = 3
    job.downloaded = 999

    eng._sync_plan(d, _tracks())

    assert eng._seg_done == 0
    assert job.downloaded == 0


def test_the_restart_flag_is_consumed(scratch):
    """It must not wipe again on every later resume."""
    job, d = scratch
    job.media_meta["restart_stream"] = True
    eng = _engine(job)

    eng._sync_plan(d, _tracks())

    assert "restart_stream" not in job.media_meta


def test_changing_quality_discards_segments_of_the_other_resolution(scratch):
    job, d = scratch
    eng = _engine(job)
    eng._sync_plan(d, _tracks())                     # stamp the 1080p plan
    for i in range(3):                               # pretend they downloaded
        (d / "video" / f"{i:06d}.seg").write_bytes(b"OLD-1080p")

    job.media_meta["height"] = 480                   # user picks a new quality
    eng._sync_plan(d, _tracks(count=5))

    assert _segments_on_disk(d) == [], (
        "segments of the previous resolution would interleave with the new ones"
    )


def test_resuming_the_same_plan_keeps_its_segments(scratch):
    """The whole point of the scratch folder — a resume must not refetch."""
    job, d = scratch
    eng = _engine(job)
    eng._sync_plan(d, _tracks())

    eng._sync_plan(d, _tracks())                     # same plan again

    assert len(_segments_on_disk(d)) == 3, "a plain resume must keep its progress"


def test_a_never_stamped_folder_is_adopted_not_wiped(scratch):
    """Upgrading mid-download must not throw away gigabytes."""
    job, d = scratch
    eng = _engine(job)

    eng._sync_plan(d, _tracks())                     # no plan.json existed

    assert len(_segments_on_disk(d)) == 3
    assert (d / "plan.json").exists()


def test_the_stamp_ignores_signed_urls(scratch):
    """CDNs re-sign URLs every run; keying on them would wipe valid progress."""
    job, d = scratch
    eng = _engine(job)
    first = _tracks()
    eng._sync_plan(d, first)

    fresh = _tracks()
    for s in fresh[0].segments:
        s.url += "?token=NEWLY-SIGNED"
    eng._sync_plan(d, fresh)

    assert len(_segments_on_disk(d)) == 3, "a re-signed URL is the same download"


def test_the_stamp_records_the_plan(scratch):
    job, d = scratch
    eng = _engine(job)

    eng._sync_plan(d, _tracks())

    stamp = json.loads((d / "plan.json").read_text(encoding="utf-8"))
    assert stamp["height"] == 1080
    assert stamp["tracks"] == [{"kind": "video", "fmp4": False, "count": 3}]


def test_run_syncs_the_plan_before_downloading_anything(scratch, monkeypatch):
    """The guard is worthless if run() never calls it.

    (Mutation testing caught exactly this: deleting the call from run() failed
    no test, because every other test here calls _sync_plan directly.)
    """
    job, d = scratch
    eng = _engine(job)
    order: list[str] = []

    tracks = _tracks()
    eng._plan = lambda: tracks
    eng._cancelled = lambda: False
    eng._emit = lambda: None
    eng._set_progress_meta = lambda: None
    eng._tmp_dir = lambda: d
    eng.pause_event = threading.Event()
    eng.pause_event.set()
    eng._sync_plan = lambda tmp, tr: order.append("sync")
    eng._download_track = lambda track, tmp: order.append("download")
    eng._assemble = lambda tr: order.append("assemble")
    eng._cleanup = lambda tmp: None
    job.started_at = None

    eng.run()

    assert order and order[0] == "sync", (
        "stale segments must be discarded before any segment is fetched"
    )
    assert "download" in order


# ------------------------------------------------------- the manager side

def test_redownload_asks_the_engine_to_restart(data_dir, make_job, monkeypatch):
    from magic_downloader.manager import DownloadManager

    mgr = DownloadManager()
    job = make_job(filename="clip.mp4")
    job.media_type = "hls"
    job.media_meta = {"height": 720, "seg_done": 12}
    mgr.jobs.append(job)
    monkeypatch.setattr(DownloadManager, "start_job", lambda self, jid, **kw: None)

    mgr.redownload_job(job.id)

    assert job.media_meta.get("restart_stream") is True
    assert "seg_done" not in job.media_meta


def test_changing_quality_asks_the_engine_to_restart(data_dir, make_job, monkeypatch):
    from magic_downloader.manager import DownloadManager

    mgr = DownloadManager()
    job = make_job(filename="clip.mp4")
    job.media_type = "hls"
    job.media_meta = {"height": 1080}
    mgr.jobs.append(job)
    monkeypatch.setattr(DownloadManager, "start_job", lambda self, jid, **kw: None)

    mgr.set_job_quality(job.id, {"height": 480})

    assert job.media_meta["height"] == 480
    assert job.media_meta.get("restart_stream") is True


def test_changing_quality_stops_the_running_engine_first(data_dir, make_job, monkeypatch):
    """Two engines on one job would write into the same scratch folder."""
    from magic_downloader.manager import DownloadManager

    mgr = DownloadManager()
    job = make_job(filename="clip.mp4")
    job.media_type = "hls"
    job.status = DownloadStatus.DOWNLOADING
    mgr.jobs.append(job)
    cancelled = []
    monkeypatch.setattr(DownloadManager, "cancel_job", lambda self, jid: cancelled.append(jid))
    monkeypatch.setattr(DownloadManager, "start_job", lambda self, jid, **kw: None)

    mgr.set_job_quality(job.id, {"height": 480})

    assert cancelled == [job.id]
