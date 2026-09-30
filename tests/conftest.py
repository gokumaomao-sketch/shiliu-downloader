"""Shared test fixtures.

The app resolves its data paths at import time (``config.SETTINGS_PATH`` and
``config.JOBS_PATH`` are module-level constants built from ``paths.DATA_DIR``),
and ``storage`` imports ``JOBS_PATH`` *by value*. So isolating a test from the
developer's real download history means redirecting all of those bindings, not
just one — hence the fixture below rather than a single monkeypatch in each test.

Nothing here may touch %LOCALAPPDATA%\\MagicDownloader: that is the user's real
data.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

# Import the package from the repo root without needing an install step.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _no_background_scheduler(monkeypatch):
    """Keep a DownloadManager's queue thread from outliving the test that made it.

    Every DownloadManager starts a daemon scheduler that re-persists the job list
    every couple of seconds, and nothing ever stops it. The persistence paths are
    module globals redirected per test, so a manager leaked by an earlier test
    goes on writing — into a LATER test's data directory. That silently rewrote
    the deliberately-corrupted jobs.json in the recovery test, which failed about
    one run in four (and pytest-randomly reshuffles the order every run, so it
    moved around). Neutralise the loop; tests that want it drive it directly.
    """
    from magic_downloader import manager as manager_mod

    monkeypatch.setattr(manager_mod.DownloadManager, "_schedule_loop", lambda self: None)
    yield
    # start_job runs each download on a "dl-<id>" thread whose runner persists
    # the job list once run() returns. That write goes to the module-global
    # JOBS_PATH, which the next test redirects to ITS own directory — so a
    # straggler from this test would rewrite the next test's jobs.json. Wait for
    # them here rather than leaving a landmine for whichever test runs next.
    deadline = time.monotonic() + 5.0
    for thread in threading.enumerate():
        if thread is threading.current_thread() or not thread.is_alive():
            continue
        if thread.name.startswith("dl-"):
            thread.join(timeout=max(0.0, deadline - time.monotonic()))


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point every persistence path at a throwaway directory.

    Yields the data directory. Any test that loads or saves jobs/settings must
    request this fixture, or it will read and overwrite real user data.
    """
    import magic_downloader.config as config
    import magic_downloader.paths as paths
    import magic_downloader.storage as storage

    data = tmp_path / "data"
    downloads = tmp_path / "downloads"
    data.mkdir(parents=True, exist_ok=True)
    downloads.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(paths, "DATA_DIR", data, raising=False)
    monkeypatch.setattr(paths, "DOWNLOADS_DIR", downloads, raising=False)

    monkeypatch.setattr(config, "DATA_DIR", data, raising=False)
    monkeypatch.setattr(config, "DOWNLOADS_DIR", downloads, raising=False)
    monkeypatch.setattr(config, "SETTINGS_PATH", data / "settings.json")
    monkeypatch.setattr(config, "JOBS_PATH", data / "jobs.json")
    # storage did `from ...config import JOBS_PATH`, a separate binding.
    monkeypatch.setattr(storage, "JOBS_PATH", data / "jobs.json")

    # The real ensure_dirs() creates the user's category folders (Music, Video …)
    # under their home directory. Keep tests off the real filesystem.
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(storage, "ensure_dirs", lambda: None)

    return data


@pytest.fixture
def settings(data_dir):
    """A fresh copy of the default settings, isolated from disk."""
    import copy

    import magic_downloader.config as config

    return copy.deepcopy(config.DEFAULT_SETTINGS)


@pytest.fixture
def make_job():
    """Factory for a DownloadJob with sensible test defaults."""
    from magic_downloader.models import DownloadJob

    def _make(filename="file.bin", url="https://example.test/file.bin", **kw):
        job = DownloadJob(url=url, save_path=str(Path("C:/dl") / filename), filename=filename)
        for k, v in kw.items():
            setattr(job, k, v)
        return job

    return _make


class FakeResponse:
    """Minimal stand-in for a requests.Response used by engine probes."""

    def __init__(self, url="https://example.test/f", headers=None, status_code=200, body=b""):
        self.url = url
        self.headers = headers or {}
        self.status_code = status_code
        self._body = body
        self.closed = False

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeSession:
    """requests.Session stand-in that answers with one canned FakeResponse.

    One exception, so the fake behaves like a real origin: a request carrying a
    ``Range`` header is answered **206** when the canned response advertises
    ``Accept-Ranges: bytes``. ``probe()`` verifies range support by actually
    sending ``bytes=0-0`` and believes what the server *does* over what it
    advertises — a fake that answered 200 to that would look exactly like a
    server which ignores ranges, and range support would (correctly) be turned
    off. Tests that want a range-*ignoring* server drive one explicitly.
    """

    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.headers = {}
        self.calls = []

    def head(self, url, **kw):
        self.calls.append(("head", url, kw))
        return self.response

    def get(self, url, **kw):
        self.calls.append(("get", url, kw))
        if "Range" in (kw.get("headers") or {}):
            accept = str(self.response.headers.get("Accept-Ranges", "")).lower()
            if accept and accept != "none":
                return FakeResponse(
                    url=self.response.url,
                    headers=dict(self.response.headers),
                    status_code=206,
                )
        return self.response


@pytest.fixture
def fake_response():
    return FakeResponse


@pytest.fixture
def fake_session():
    return FakeSession
