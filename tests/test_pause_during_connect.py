"""Pausing while a download is still connecting must actually pause it.

run() used to set DOWNLOADING unconditionally once probe()/_plan() returned,
checking only for cancel. A pause during the connect phase was therefore
overwritten: the workers blocked inside _wait_if_paused() and never reached
their own PAUSED check, so the row showed "Downloading" at 0 B/s forever — and
because DOWNLOADING counts as busy, it permanently held one of the
max_simultaneous slots, so nothing queued behind it could ever start.

The risky part of the fix is Resume, which goes through the same code path as
the queue scheduler, so it is covered here too.
"""

from __future__ import annotations

import threading

import pytest

from magic_downloader.engine import DownloadEngine
from magic_downloader.models import DownloadStatus


class _Probe:
    """Stands in for probe(): marks CONNECTING, like the real one does."""

    def __init__(self, job, pause_event, pause_during=False):
        self.job = job
        self.pause_event = pause_event
        self.pause_during = pause_during
        self.called = False

    def __call__(self):
        self.called = True
        self.job.status = DownloadStatus.CONNECTING
        if self.pause_during:            # the user hits Pause while connecting
            self.pause_event.clear()
            self.job.status = DownloadStatus.PAUSED


def _engine(job, pause_event, pause_during):
    eng = DownloadEngine.__new__(DownloadEngine)
    eng.job = job
    eng.pause_event = pause_event
    eng._stop = False
    eng.cancel_check = lambda: False
    eng.on_progress = None
    eng._emit = lambda: None
    eng._lock = threading.RLock()
    eng.probe = _Probe(job, pause_event, pause_during)
    eng._run_multipart = lambda: pytest.fail("must not start transferring while paused")
    eng._run_single = lambda: pytest.fail("must not start transferring while paused")
    eng._finalize = lambda: None
    return eng


@pytest.fixture
def job(make_job, tmp_path):
    j = make_job(save_path=str(tmp_path / "f.bin"))
    j.total_size = 0        # force probe()
    j.segments = []
    j.connections = 4
    j.started_at = None
    return j


def test_pause_during_connect_leaves_the_job_paused(job):
    pe = threading.Event()
    pe.set()
    eng = _engine(job, pe, pause_during=True)

    eng.run()

    assert job.status == DownloadStatus.PAUSED, (
        "a pause during the connect phase was overwritten with DOWNLOADING"
    )


def test_pause_during_connect_does_not_report_a_speed(job):
    pe = threading.Event()
    pe.set()
    eng = _engine(job, pe, pause_during=True)

    eng.run()

    assert job.speed_bps == 0.0


def test_pause_during_connect_frees_the_queue_slot(job):
    """DOWNLOADING counts as busy; a stuck job blocked the whole queue."""
    from magic_downloader.manager import BUSY_STATUSES

    pe = threading.Event()
    pe.set()
    eng = _engine(job, pe, pause_during=True)

    eng.run()

    assert job.status not in BUSY_STATUSES, "a paused job must not occupy a slot"


def test_a_normal_start_still_downloads(job):
    """Guard: the new check must not block ordinary downloads."""
    pe = threading.Event()
    pe.set()
    eng = _engine(job, pe, pause_during=False)
    ran = []
    eng._run_multipart = lambda: ran.append("multipart")
    eng._run_single = lambda: ran.append("single")
    job.total_size = 1000
    job.supports_ranges = True

    eng.run()

    assert ran, "an unpaused job must still transfer"
    assert eng.probe.called


# ------------------------------------------------------- the manager side

def test_the_scheduler_does_not_restart_a_job_paused_in_the_meantime(data_dir, make_job):
    """_kick_queue starts jobs on another thread; the user may pause first."""
    from magic_downloader.manager import DownloadManager

    mgr = DownloadManager()
    j = make_job(filename="q.bin")
    j.status = DownloadStatus.PAUSED
    mgr.jobs.append(j)

    mgr.start_job(j.id, from_queue=True)

    assert j.status == DownloadStatus.PAUSED, (
        "the scheduler must not undo a pause that happened after it chose the job"
    )
    assert j.id not in mgr._threads


def test_an_explicit_resume_still_starts_a_paused_job(data_dir, make_job, monkeypatch):
    """The dangerous half: Resume uses the same entry point as the scheduler."""
    from magic_downloader.manager import DownloadManager

    mgr = DownloadManager()
    j = make_job(filename="r.bin")
    j.status = DownloadStatus.PAUSED
    mgr.jobs.append(j)

    started = threading.Event()
    monkeypatch.setattr(
        DownloadEngine, "run", lambda self: started.set(), raising=False
    )

    mgr.start_job(j.id)          # no from_queue → a user action

    started.wait(timeout=5)
    assert started.is_set(), "Resume must still start a paused job"


def test_the_scheduler_still_starts_a_queued_job(data_dir, make_job, monkeypatch):
    """Guard: the new check must only reject PAUSED/CANCELLED."""
    from magic_downloader.manager import DownloadManager

    mgr = DownloadManager()
    j = make_job(filename="s.bin")
    j.status = DownloadStatus.QUEUED
    mgr.jobs.append(j)

    started = threading.Event()
    monkeypatch.setattr(
        DownloadEngine, "run", lambda self: started.set(), raising=False
    )

    mgr.start_job(j.id, from_queue=True)

    started.wait(timeout=5)
    assert started.is_set(), "a genuinely queued job must still start"
