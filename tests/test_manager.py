"""DownloadManager behaviour that has no business touching the network.

Everything here runs against a manager whose ``start_job``/``_kick_queue`` are
stubbed out, so no engine is ever constructed and no socket is ever opened. The
areas covered are the ones with a history of user-visible breakage:

* ``suggest_capture`` — name/category/folder derivation and the "Ask each time"
  video-quality rules;
* ``delete_job`` — the multi-GB stream scratch folder must always go;
* duplicate collapsing by save path (list showed two rows for one file);
* ``_on_progress`` throttling — the fix for the app freezing on slow machines;
* pause / cancel / retry / redownload status transitions.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import magic_downloader.manager as mm
from magic_downloader.models import DownloadStatus

CATEGORIES = ("General", "Compressed", "Documents", "Music", "Video")

#: A stream capture payload with nothing that would pin a quality.
STREAM = {
    "url": "https://cdn.test/hls/master.m3u8",
    "title": "My Show S01E01",
    "page_url": "https://site.test/watch",
}


class FakeEngine:
    """Stand-in for DownloadEngine: records the control calls made on it."""

    def __init__(self, stop_raises: bool = False) -> None:
        self.stopped = False
        self.paused = False
        self.resumed = False
        self._stop_raises = stop_raises

    def stop(self) -> None:
        self.stopped = True
        if self._stop_raises:
            raise RuntimeError("engine refused to stop")

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.resumed = True


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Never let a junk filename trigger a real HEAD/GET against the internet."""
    calls: list[tuple] = []

    def _blocked(url, cookie="", referrer="", user_agent="", timeout=12):
        calls.append((url, cookie, referrer, user_agent))
        return "", 0

    monkeypatch.setattr(mm, "resolve_download_name", _blocked)
    return calls


@pytest.fixture
def make_manager(data_dir, tmp_path, monkeypatch):
    """Factory for a DownloadManager that can never launch a real download.

    ``start_job`` is replaced by a recorder (``manager.started``) and the queue
    scheduler is neutered, so a QUEUED job left behind by a test can't be picked
    up two seconds later and actually downloaded.
    """
    built: list = []

    def _make():
        started: list[str] = []
        monkeypatch.setattr(mm.DownloadManager, "start_job", lambda self, jid: started.append(jid))
        monkeypatch.setattr(mm.DownloadManager, "_kick_queue", lambda self: None)
        m = mm.DownloadManager()
        m.started = started
        # DEFAULT_SETTINGS was built at import time from the *real* Downloads
        # folder; resolve_save_path() mkdirs whatever it is handed, so redirect
        # every category into tmp_path before anything calls it.
        downloads = tmp_path / "dl"
        m.settings["default_save_path"] = str(downloads)
        m.settings["category_paths"] = {c: str(downloads / c) for c in CATEGORIES}
        built.append(m)
        return m

    yield _make
    for m in built:
        m.shutdown()


@pytest.fixture
def manager(make_manager):
    return make_manager()


def _job_at(make_job, tmp_path, name, **attrs):
    """A job whose save_path is a real path inside tmp_path."""
    target = tmp_path / "dl" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    job = make_job(filename=name, **attrs)
    job.save_path = str(target)
    return job, target


# ── suggest_capture: name / category / folder ────────────────────────


def test_plain_file_name_category_and_folder(manager, tmp_path):
    sug = manager.suggest_capture({"url": "https://example.test/docs/manual%20v2.pdf"})

    assert sug["filename"] == "manual v2.pdf"
    assert sug["category"] == "Documents"
    assert Path(sug["folder"]) == tmp_path / "dl" / "Documents"
    assert sug["media_type"] == "http"
    assert sug["is_stream"] is False
    assert sug["media_meta"] == {}


def test_supplied_filename_beats_the_url(manager, tmp_path):
    sug = manager.suggest_capture({"url": "https://example.test/x.bin", "filename": "song.mp3"})

    assert sug["filename"] == "song.mp3"
    assert sug["category"] == "Music", "category follows the real name, not the URL"
    assert Path(sug["folder"]) == tmp_path / "dl" / "Music"


def test_video_filename_uses_page_title(manager):
    sug = manager.suggest_capture({
        "url": "https://cdn.example/02d1666a-173a.mp4",
        "filename": "02d1666a-173a.mp4",
        "title": "追日/全食是什么体验？",
    })
    assert sug["filename"] == "追日_全食是什么体验？.mp4"
    assert sug["category"] == "Video"


def test_explicit_category_overrides_the_extension_mapping(manager, tmp_path):
    sug = manager.suggest_capture({"url": "https://example.test/a.pdf", "category": "Video"})

    assert sug["category"] == "Video"
    assert Path(sug["folder"]) == tmp_path / "dl" / "Video"


def test_illegal_path_characters_are_replaced(manager):
    sug = manager.suggest_capture(
        {"url": "https://e.test/a.bin", "filename": 'AC/DC: "Live"|1979?.mp3'}
    )

    assert sug["filename"] == "AC_DC_ _Live__1979_.mp3"
    assert "/" not in sug["filename"] and "\\" not in sug["filename"]


def test_empty_name_falls_back_to_download(manager):
    sug = manager.suggest_capture({"url": "https://e.test/", "filename": "   "})

    assert sug["filename"] == "download"


def test_a_relative_name_cannot_escape_the_target_folder(manager):
    sug = manager.suggest_capture({"url": "https://e.test/x", "filename": "../../evil"})

    assert sug["filename"] == ".._.._evil"
    assert Path(sug["filename"]).name == sug["filename"]


def test_junk_name_is_resolved_from_the_server(manager, monkeypatch):
    seen: list[tuple] = []

    def fake(url, cookie="", referrer="", user_agent="", timeout=12):
        seen.append((url, cookie, referrer, user_agent))
        return "Quarterly Report.pdf", 4242

    monkeypatch.setattr(mm, "resolve_download_name", fake)

    sug = manager.suggest_capture(
        {
            "url": "https://e.test/a5de8a50-7e22-4163-97a4-914d6644caa7",
            "cookies": "sid=1",
            "referer": "https://e.test/page",
        }
    )

    assert sug["filename"] == "Quarterly Report.pdf"
    assert sug["size"] == 4242
    assert sug["category"] == "Documents", "the resolved extension must drive the category"
    assert seen and seen[0][1] == "sid=1" and seen[0][2] == "https://e.test/page"


def test_a_good_name_is_not_probed(manager, no_network):
    manager.suggest_capture({"url": "https://e.test/archive.zip"})

    assert no_network == [], "a usable filename must not cost a network round-trip"


def test_probe_failure_falls_back_to_the_url_name(manager, monkeypatch):
    def boom(*a, **k):
        raise OSError("no route to host")

    monkeypatch.setattr(mm, "resolve_download_name", boom)

    sug = manager.suggest_capture({"url": "https://e.test/blob"})

    assert sug["filename"] == "blob"
    assert sug["size"] == 0


def test_a_junk_resolved_name_is_ignored_but_its_size_is_kept(manager, monkeypatch):
    monkeypatch.setattr(mm, "resolve_download_name", lambda *a, **k: ("download", 99))

    sug = manager.suggest_capture({"url": "https://e.test/blob"})

    assert sug["filename"] == "blob"
    assert sug["size"] == 99


# ── suggest_capture: streams ─────────────────────────────────────────


def test_hls_capture_is_a_video_stream(manager, tmp_path):
    sug = manager.suggest_capture({**STREAM, "title": "Big Buck Bunny.mp4"})

    assert sug["media_type"] == "hls"
    assert sug["is_stream"] is True
    assert sug["filename"] == "Big Buck Bunny.mp4", "the manifest extension must be stripped once"
    assert sug["category"] == "Video"
    assert Path(sug["folder"]) == tmp_path / "dl" / "Video"


def test_audio_only_stream_becomes_m4a_in_music(manager, tmp_path):
    sug = manager.suggest_capture({**STREAM, "title": "Podcast Ep 3", "audio_only": True})

    assert sug["filename"] == "Podcast Ep 3.m4a"
    assert sug["category"] == "Music"
    assert Path(sug["folder"]) == tmp_path / "dl" / "Music"
    assert sug["media_meta"]["audio_only"] is True
    assert sug["audio_only"] is True


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"url": "https://cdn.test/manifest.mpd"}, "dash"),
        ({"url": "https://cdn.test/v/index.m3u8?token=1"}, "hls"),
        ({"url": "https://site.test/watch?v=abc", "media_type": "page"}, "page"),
        ({"url": "https://e.test/clip.mp4"}, "http"),
    ],
)
def test_media_type_detection(manager, data, expected):
    sug = manager.suggest_capture(data)

    assert sug["media_type"] == expected
    assert sug["is_stream"] is (expected != "http")


def test_untitled_stream_gets_a_generic_name(manager):
    sug = manager.suggest_capture({"url": "https://cdn.test/manifest.mpd"})

    assert sug["filename"] == "video.mp4"


def test_stream_meta_carries_title_page_url_duration_and_height(manager):
    meta = manager.suggest_capture(
        {**STREAM, "duration": "125.7", "height": "1080", "quality": "1080p"}
    )["media_meta"]

    assert meta["title"] == "My Show S01E01"
    assert meta["page_url"] == "https://site.test/watch"
    assert meta["duration"] == 125
    assert meta["height"] == 1080
    assert meta["quality"] == "1080p"


def test_unparseable_stream_numbers_are_dropped(manager):
    meta = manager.suggest_capture({**STREAM, "duration": "soon", "height": "big"})["media_meta"]

    assert "duration" not in meta
    assert "height" not in meta


# ── suggest_capture: the "Ask each time" quality rules ───────────────


def test_ask_quality_is_flagged_when_the_default_is_ask(manager):
    manager.settings["default_video_quality"] = "ask"

    meta = manager.suggest_capture(dict(STREAM))["media_meta"]

    assert meta.get("ask_quality") is True
    assert "height" not in meta, "asking and pre-picking a height are mutually exclusive"


@pytest.mark.parametrize("dq,height", [("2160", 2160), ("1080", 1080), ("480", 480)])
def test_a_numeric_default_quality_pins_the_height(manager, dq, height):
    manager.settings["default_video_quality"] = dq

    meta = manager.suggest_capture(dict(STREAM))["media_meta"]

    assert meta["height"] == height
    assert "ask_quality" not in meta


@pytest.mark.parametrize("dq", ["best", "", None])
def test_best_default_quality_pins_nothing(manager, dq):
    manager.settings["default_video_quality"] = dq

    meta = manager.suggest_capture(dict(STREAM))["media_meta"]

    assert "ask_quality" not in meta
    assert "height" not in meta


@pytest.mark.parametrize(
    "explicit",
    [
        {"best": True},
        {"format_id": "137+140"},
        {"height": 720},
        {"audio_only": True},
    ],
    ids=["star-best", "format-id", "height", "audio-only"],
)
def test_an_explicit_choice_suppresses_the_quality_prompt(manager, explicit):
    manager.settings["default_video_quality"] = "ask"

    meta = manager.suggest_capture({**STREAM, **explicit})["media_meta"]

    assert "ask_quality" not in meta, f"{explicit} already says which quality to take"


def test_star_best_also_beats_a_numeric_default(manager):
    manager.settings["default_video_quality"] = "1080"

    meta = manager.suggest_capture({**STREAM, "best": True})["media_meta"]

    assert "height" not in meta
    assert "ask_quality" not in meta


def test_a_plain_file_never_gets_a_quality_prompt(manager):
    manager.settings["default_video_quality"] = "ask"

    sug = manager.suggest_capture({"url": "https://example.test/setup.exe"})

    assert sug["media_meta"] == {}


# ── suggest_capture: misc payload handling ───────────────────────────


@pytest.mark.parametrize(
    "given,expected", [(99, 32), (4, 4), (-5, 1), ("12", 12), (None, 6), (0, 6)]
)
def test_connections_are_clamped_and_defaulted(manager, given, expected):
    manager.settings["connections"] = 6

    sug = manager.suggest_capture({"url": "https://e.test/a.bin", "connections": given})

    assert sug["connections"] == expected


def test_cookie_referrer_and_header_aliases(manager):
    sug = manager.suggest_capture(
        {
            "url": "https://e.test/a.bin",
            "cookies": "a=1",
            "referer": "https://r.test/p",
            "extra_headers": {"X-N": 5},
        }
    )

    assert sug["cookie"] == "a=1"
    assert sug["referrer"] == "https://r.test/p"
    assert sug["extra_headers"] == {"X-N": "5"}, "header values must be stringified"


def test_page_url_is_the_last_referrer_fallback(manager):
    sug = manager.suggest_capture({"url": "https://e.test/a.bin", "page_url": "https://p.test/x"})

    assert sug["referrer"] == "https://p.test/x"


def test_non_dict_headers_are_ignored(manager):
    sug = manager.suggest_capture({"url": "https://e.test/a.bin", "headers": ["nope"]})

    assert sug["extra_headers"] == {}


def test_suggest_capture_has_no_side_effects(manager):
    manager.suggest_capture({"url": "https://e.test/a.zip"})
    manager.suggest_capture(dict(STREAM))

    assert manager.jobs == []
    assert manager.started == [], "suggesting must never queue or start anything"


# ── add_job / duplicate collapsing ───────────────────────────────────


def test_add_job_replaces_an_entry_with_the_same_save_path(manager, make_job):
    old = make_job(filename="same.iso")
    other = make_job(filename="other.iso")
    new = make_job(filename="same.iso")

    manager.add_job(old, start=False)
    manager.add_job(other, start=False)
    manager.add_job(new, start=False)

    assert [j.id for j in manager.jobs] == [new.id, other.id]


def test_add_job_stops_and_forgets_the_replaced_entry(manager, make_job):
    old = make_job(filename="dup.bin")
    manager.add_job(old, start=False)
    engine = FakeEngine()
    manager._engines[old.id] = engine
    manager._threads[old.id] = object()
    manager._pause_events[old.id] = threading.Event()

    manager.add_job(make_job(filename="dup.bin"), start=False)

    assert engine.stopped, "the replaced download must be told to stop"
    assert old.id not in manager._engines
    assert old.id not in manager._threads
    assert old.id not in manager._pause_events


def test_a_stubborn_engine_does_not_block_the_replacement(manager, make_job):
    old = make_job(filename="dup.bin")
    manager.add_job(old, start=False)
    manager._engines[old.id] = FakeEngine(stop_raises=True)

    new = make_job(filename="dup.bin")
    manager.add_job(new, start=False)

    assert [j.id for j in manager.jobs] == [new.id]


def test_add_job_starts_only_when_asked(manager, make_job):
    quiet = make_job(filename="quiet.bin")
    loud = make_job(filename="loud.bin")

    manager.add_job(quiet, start=False)
    assert manager.started == []

    manager.add_job(loud, start=True)
    assert manager.started == [loud.id]


def test_add_job_persists_straight_away(manager, make_job):
    from magic_downloader import storage

    job = make_job(filename="persisted.bin")
    manager.add_job(job, start=False)

    assert [j.id for j in storage.load_jobs()] == [job.id]


def test_dedupe_keeps_the_completed_copy(make_job):
    partial = make_job(filename="x.iso", downloaded=10)
    done = make_job(filename="x.iso", downloaded=0, status=DownloadStatus.COMPLETE)

    kept = mm._dedupe_jobs_by_path([partial, done])

    assert [j.id for j in kept] == [done.id]


def test_dedupe_keeps_the_furthest_along_when_none_completed(make_job):
    barely = make_job(filename="x.iso", downloaded=5)
    nearly = make_job(filename="x.iso", downloaded=500)

    kept = mm._dedupe_jobs_by_path([barely, nearly])

    assert [j.id for j in kept] == [nearly.id]


def test_dedupe_keeps_distinct_paths_and_preserves_order(make_job):
    a = make_job(filename="x.iso", downloaded=0)
    b = make_job(filename="y.iso")
    c = make_job(filename="x.iso", downloaded=99)

    kept = mm._dedupe_jobs_by_path([a, b, c])

    assert [j.id for j in kept] == [b.id, c.id]


def test_manager_collapses_duplicates_it_loads_from_disk(make_manager):
    from magic_downloader import storage

    storage.save_jobs_payload(
        [
            {"id": "a", "url": "u", "save_path": "C:/dl/f.iso", "filename": "f.iso", "downloaded": 5},
            {"id": "b", "url": "u", "save_path": "C:/dl/f.iso", "filename": "f.iso", "downloaded": 900},
            {"id": "c", "url": "u", "save_path": "C:/dl/g.iso", "filename": "g.iso"},
        ]
    )

    m = make_manager()

    assert [j.id for j in m.jobs] == ["b", "c"]


# ── delete_job ───────────────────────────────────────────────────────


def test_delete_job_always_removes_the_stream_scratch_folder(manager, make_job, tmp_path):
    job, target = _job_at(make_job, tmp_path, "movie.mp4")
    target.write_bytes(b"finished")
    manager.jobs = [job]
    scratch = tmp_path / "dl" / f"movie.mp4.mdtmp-{job.id}"
    (scratch / "segments").mkdir(parents=True)
    (scratch / "segments" / "0.ts").write_bytes(b"x" * 64)

    manager.delete_job(job.id)

    assert manager.get_job(job.id) is None
    assert not scratch.exists(), "the (potentially multi-GB) segment folder must go"
    assert target.exists(), "delete_files=False must leave the downloaded file alone"


def test_delete_job_with_delete_files_removes_file_part_and_scratch(manager, make_job, tmp_path):
    job, target = _job_at(make_job, tmp_path, "clip.mp4")
    target.write_bytes(b"whole")
    part = Path(str(target) + ".part")
    part.write_bytes(b"half")
    scratch = tmp_path / "dl" / f"clip.mp4.mdtmp-{job.id}"
    scratch.mkdir(parents=True)
    (scratch / "0.ts").write_bytes(b"seg")
    manager.jobs = [job]

    manager.delete_job(job.id, delete_files=True)

    assert not target.exists()
    assert not part.exists()
    assert not scratch.exists()
    assert manager.jobs == []


def test_delete_job_without_a_scratch_folder_is_fine(manager, make_job, tmp_path):
    job, _target = _job_at(make_job, tmp_path, "plain.bin")
    manager.jobs = [job]

    manager.delete_job(job.id)

    assert manager.jobs == []


def test_delete_job_only_removes_the_named_job(manager, make_job, tmp_path):
    doomed, _ = _job_at(make_job, tmp_path, "a.bin")
    keeper, keeper_path = _job_at(make_job, tmp_path, "b.bin")
    keeper_path.write_bytes(b"safe")
    keeper_scratch = tmp_path / "dl" / f"b.bin.mdtmp-{keeper.id}"
    keeper_scratch.mkdir(parents=True)
    manager.jobs = [doomed, keeper]

    manager.delete_job(doomed.id, delete_files=True)

    assert [j.id for j in manager.jobs] == [keeper.id]
    assert keeper_path.exists()
    assert keeper_scratch.exists()


def test_delete_job_persists_the_shorter_list(manager, make_job, tmp_path):
    from magic_downloader import storage

    job, _ = _job_at(make_job, tmp_path, "gone.bin")
    manager.add_job(job, start=False)

    manager.delete_job(job.id)

    assert storage.load_jobs() == []


def test_delete_unknown_job_is_a_no_op(manager, make_job):
    job = make_job()
    manager.jobs = [job]

    manager.delete_job("does-not-exist", delete_files=True)

    assert manager.jobs == [job]


# ── _on_progress / _persist throttling ───────────────────────────────


@pytest.fixture
def counted(manager, monkeypatch, make_job):
    """A manager whose _notify and disk writes are counted instead of done."""
    notifies: list[float] = []
    writes: list[list] = []
    monkeypatch.setattr(manager, "_notify", lambda: notifies.append(time.monotonic()))
    monkeypatch.setattr(mm, "save_jobs_payload", lambda payload: writes.append(payload))
    job = make_job()
    manager.jobs = [job]
    manager._last_progress_notify = 0.0
    manager._persist_timer = 0.0
    return manager, job, notifies, writes


def test_on_progress_collapses_a_burst_into_one_notify(counted):
    manager, job, notifies, _writes = counted

    manager._on_progress(job)
    assert len(notifies) == 1, "the first call claims the slot"

    for _ in range(50):
        manager._on_progress(job)

    assert len(notifies) == 1, "everything inside the 120ms window must be dropped"


def test_on_progress_resumes_after_the_window(counted):
    manager, job, notifies, _writes = counted

    manager._on_progress(job)
    time.sleep(0.15)
    manager._on_progress(job)

    assert len(notifies) == 2, "progress must still reach the GUI, just less often"


def test_on_progress_is_cheap_when_hammered_from_many_threads(counted):
    """20k progress callbacks (what a 16-connection download really produces)
    must not turn into 20k GUI refreshes or 20k jobs.json writes."""
    manager, job, notifies, writes = counted
    threads_done: list[int] = []
    per_thread = 2500
    workers = 8

    def hammer(n: int) -> None:
        for _ in range(per_thread):
            manager._on_progress(job)
        threads_done.append(n)

    started = time.monotonic()
    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    elapsed = time.monotonic() - started

    assert len(threads_done) == workers, "all 20000 calls must have completed"
    assert not any(t.is_alive() for t in threads)
    # Unthrottled this would be 20000 notifies and thousands of writes.
    assert 1 <= len(notifies) <= int(elapsed / 0.12) + 2
    assert len(notifies) < 100
    assert 1 <= len(writes) <= int(elapsed) + 2


def test_persist_is_debounced_but_force_always_writes(counted):
    manager, _job, _notifies, writes = counted

    manager._persist(force=False)
    assert len(writes) == 1

    for _ in range(100):
        manager._persist(force=False)
    assert len(writes) == 1, "a second within the debounce window means no second write"

    manager._persist(force=True)
    assert len(writes) == 2, "force must bypass the debounce"


def test_persist_writes_the_current_job_list(counted):
    manager, job, _notifies, writes = counted

    manager._persist(force=True)

    assert [row["id"] for row in writes[-1]] == [job.id]


# ── status transitions ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "status",
    [DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING, DownloadStatus.CONNECTING],
)
def test_pause_job_halts_an_active_download(manager, make_job, status):
    job = make_job(status=status)
    manager.jobs = [job]
    gate = threading.Event()
    gate.set()
    manager._pause_events[job.id] = gate
    engine = FakeEngine()
    manager._engines[job.id] = engine

    manager.pause_job(job.id)

    assert job.status == DownloadStatus.PAUSED
    assert not gate.is_set(), "workers must block on the pause gate"
    assert engine.paused


@pytest.mark.parametrize(
    "status",
    [
        DownloadStatus.COMPLETE,
        DownloadStatus.FAILED,
        DownloadStatus.CANCELLED,
        DownloadStatus.PAUSED,
        # PROCESSING = merging segments; deliberately not pausable.
        DownloadStatus.PROCESSING,
    ],
)
def test_pause_job_leaves_non_running_jobs_alone(manager, make_job, status):
    job = make_job(status=status)
    manager.jobs = [job]
    engine = FakeEngine()
    manager._engines[job.id] = engine

    manager.pause_job(job.id)

    assert job.status == status
    assert not engine.paused


@pytest.mark.parametrize(
    "status",
    [DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING, DownloadStatus.PAUSED, DownloadStatus.FAILED],
)
def test_cancel_job_stops_the_engine_and_releases_waiters(manager, make_job, status):
    job = make_job(status=status)
    manager.jobs = [job]
    gate = threading.Event()
    manager._pause_events[job.id] = gate
    engine = FakeEngine()
    manager._engines[job.id] = engine

    manager.cancel_job(job.id)

    assert job.status == DownloadStatus.CANCELLED
    assert engine.stopped
    assert gate.is_set(), "a paused worker must be woken so it can exit"


def test_cancel_job_is_unconditional_even_for_a_finished_download(manager, make_job):
    """Current behaviour, pinned deliberately: cancel_job does not check status,
    so cancelling an already-complete job re-labels it Cancelled."""
    job = make_job(status=DownloadStatus.COMPLETE)
    manager.jobs = [job]

    manager.cancel_job(job.id)

    assert job.status == DownloadStatus.CANCELLED


def test_cancelled_job_reports_itself_cancelled_to_the_engine(manager, make_job):
    job = make_job(status=DownloadStatus.DOWNLOADING)
    manager.jobs = [job]

    assert manager._is_cancelled(job.id) is False
    manager.cancel_job(job.id)
    assert manager._is_cancelled(job.id) is True


def test_a_deleted_job_reads_as_cancelled(manager):
    assert manager._is_cancelled("no-such-job") is True, (
        "a running engine must abort once its job is gone from the list"
    )


@pytest.mark.parametrize(
    "status",
    [DownloadStatus.FAILED, DownloadStatus.CANCELLED, DownloadStatus.PAUSED],
)
def test_retry_requeues_clears_the_error_and_restarts(manager, make_job, status):
    job = make_job(status=status, error="Connection reset")
    manager.jobs = [job]

    manager.retry_job(job.id)

    assert job.status == DownloadStatus.QUEUED
    assert job.error == ""
    assert manager.started == [job.id]


def test_retry_keeps_partial_progress_for_resume(manager, make_job):
    job = make_job(status=DownloadStatus.FAILED, downloaded=1234, total_size=9999)
    manager.jobs = [job]

    manager.retry_job(job.id)

    assert job.downloaded == 1234, "retry must resume, not restart"
    assert job.total_size == 9999


def test_retry_does_not_requeue_a_finished_download(manager, make_job):
    job = make_job(status=DownloadStatus.COMPLETE)
    manager.jobs = [job]

    manager.retry_job(job.id)

    assert job.status == DownloadStatus.COMPLETE


def test_retry_unknown_job_starts_nothing(manager):
    manager.retry_job("no-such-job")

    assert manager.started == []


def test_redownload_discards_progress_and_the_part_file(manager, make_job, tmp_path):
    job, target = _job_at(
        make_job,
        tmp_path,
        "movie.mp4",
        status=DownloadStatus.FAILED,
        downloaded=7,
        total_size=100,
        error="boom",
    )
    job.media_meta = {"seg_done": 3, "seg_total": 10}
    part = Path(str(target) + ".part")
    part.write_bytes(b"partial")
    manager.jobs = [job]

    manager.redownload_job(job.id)

    assert not part.exists(), "a redownload must not resume from the old partial"
    assert (job.downloaded, job.total_size, job.segments, job.error) == (0, 0, [], "")
    assert "seg_done" not in job.media_meta
    assert job.media_meta["seg_total"] == 10
    assert job.status == DownloadStatus.QUEUED
    assert manager.started == [job.id]


def test_set_job_quality_swaps_the_selection_and_requeues(manager, make_job):
    job = make_job(
        status=DownloadStatus.PAUSED,
        downloaded=50,
        total_size=99,
        error="nope",
        media_meta={"height": 720, "audio_only": True, "title": "keep me"},
    )
    manager.jobs = [job]

    manager.set_job_quality(job.id, {"format_id": "137+140"})

    assert job.media_meta["format_id"] == "137+140"
    assert "height" not in job.media_meta
    assert "audio_only" not in job.media_meta
    assert job.media_meta["title"] == "keep me", "unrelated metadata must survive"
    assert (job.downloaded, job.total_size, job.error) == (0, 0, "")
    assert job.status == DownloadStatus.QUEUED
    assert manager.started == [job.id]


def test_status_snapshot_counts_each_bucket(manager, make_job):
    manager.jobs = [
        make_job(filename="1.bin", status=DownloadStatus.DOWNLOADING),
        make_job(filename="2.bin", status=DownloadStatus.PROCESSING),
        make_job(filename="3.bin", status=DownloadStatus.COMPLETE),
        make_job(filename="4.bin", status=DownloadStatus.QUEUED),
        make_job(filename="5.bin", status=DownloadStatus.QUEUED),
        make_job(filename="6.bin", status=DownloadStatus.PAUSED),
    ]

    snapshot = manager.status_snapshot()
    assert {k: snapshot[k] for k in ("total", "active", "complete", "queued")} == {
        "total": 6,
        "active": 2,
        "complete": 1,
        "queued": 2,
    }
    assert len(snapshot["jobs"]) == 6


def test_shutdown_parks_running_jobs_as_paused(manager, make_job):
    running = make_job(filename="r.bin", status=DownloadStatus.DOWNLOADING)
    merging = make_job(filename="m.bin", status=DownloadStatus.PROCESSING)
    done = make_job(filename="d.bin", status=DownloadStatus.COMPLETE)
    manager.jobs = [running, merging, done]
    engine = FakeEngine()
    manager._engines[running.id] = engine

    manager.shutdown()

    assert running.status == DownloadStatus.PAUSED
    assert merging.status == DownloadStatus.PAUSED
    assert done.status == DownloadStatus.COMPLETE
    assert engine.paused
