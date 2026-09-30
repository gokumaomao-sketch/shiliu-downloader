"""Jobs and settings must survive an unclean shutdown.

This is the regression suite for the bug where a Windows restart left the grid
empty: saving used a truncating open(), so a process killed mid-write destroyed
the file, and loading silently returned an empty list.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest


def _payload(n, tag="file"):
    return [
        {
            "id": f"j{i}",
            "url": "https://example.test/x",
            "save_path": f"C:/dl/{tag}{i}.iso",
            "filename": f"{tag}{i}.iso",
        }
        for i in range(n)
    ]


def test_round_trip(data_dir):
    from magic_downloader import storage

    storage.save_jobs_payload(_payload(5))
    assert len(storage.load_jobs()) == 5


def test_no_temp_file_left_behind(data_dir):
    from magic_downloader import storage

    storage.save_jobs_payload(_payload(3))
    assert not (data_dir / "jobs.json.tmp").exists()


def test_backup_is_kept(data_dir):
    from magic_downloader import storage

    storage.save_jobs_payload(_payload(4, "a"))
    storage.save_jobs_payload(_payload(9, "b"))
    assert (data_dir / "jobs.json.bak").exists()


def test_recovers_from_backup_when_main_is_corrupt(data_dir):
    from magic_downloader import storage

    storage.save_jobs_payload(_payload(4, "a"))   # becomes the .bak
    storage.save_jobs_payload(_payload(9, "b"))
    (data_dir / "jobs.json").write_text('[{"id": "trunc', encoding="utf-8")

    assert len(storage.load_jobs()) == 4, "should fall back to the last good copy"


def test_recovers_from_backup_when_main_is_empty(data_dir):
    from magic_downloader import storage

    storage.save_jobs_payload(_payload(6, "a"))
    storage.save_jobs_payload(_payload(7, "b"))
    (data_dir / "jobs.json").write_bytes(b"")     # classic truncation

    assert len(storage.load_jobs()) == 6


def test_corrupt_file_is_preserved_not_discarded(data_dir):
    from magic_downloader import storage

    storage.save_jobs_payload(_payload(2, "a"))
    storage.save_jobs_payload(_payload(3, "b"))
    (data_dir / "jobs.json").write_text("{not json", encoding="utf-8")
    storage.load_jobs()

    assert (data_dir / "jobs.json.corrupt").exists(), "a bad file must be kept for inspection"


def test_one_bad_row_does_not_drop_the_list(data_dir):
    from magic_downloader import storage

    rows = _payload(5)
    rows.insert(2, {"nonsense": True})
    (data_dir / "jobs.json").write_text(json.dumps(rows), encoding="utf-8")

    assert len(storage.load_jobs()) >= 5


def test_missing_file_is_not_an_error(data_dir):
    from magic_downloader import storage

    assert storage.load_jobs() == []


def test_settings_round_trip_and_recovery(data_dir):
    from magic_downloader import config

    config.save_settings({**config.DEFAULT_SETTINGS, "marker": "first"})
    config.save_settings({**config.DEFAULT_SETTINGS, "marker": "second"})
    (data_dir / "settings.json").write_text("{ broken", encoding="utf-8")

    assert config.load_settings().get("marker") == "first"


def test_settings_keep_defaults_for_new_keys(data_dir):
    from magic_downloader import config

    config.save_settings({"connections": 12})
    loaded = config.load_settings()

    assert loaded["connections"] == 12
    assert "default_video_quality" in loaded, "defaults must be merged in for upgrades"


@pytest.mark.slow
def test_survives_a_process_killed_mid_write(data_dir, tmp_path):
    """The actual reported failure: kill the app while it is saving."""
    from magic_downloader import storage

    storage.save_jobs_payload(_payload(10, "safe"))

    writer = tmp_path / "writer.py"
    writer.write_text(
        "import sys\n"
        f"sys.path.insert(0, r'{sys.path[0]}')\n"
        "from pathlib import Path\n"
        "import magic_downloader.config as config\n"
        f"config.JOBS_PATH = Path(r'{data_dir / 'jobs.json'}')\n"
        "config.ensure_dirs = lambda: None\n"
        "import magic_downloader.storage as storage\n"
        "storage.JOBS_PATH = config.JOBS_PATH\n"
        "storage.ensure_dirs = lambda: None\n"
        "big = [{'id': 'x%d' % i, 'url': 'u', 'save_path': 'C:/d/b%d' % i,\n"
        "        'filename': 'b%d.bin' % i} for i in range(200000)]\n"
        "while True:\n"
        "    storage.save_jobs_payload(big)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen([sys.executable, str(writer)])
    time.sleep(2.0)          # let it get into the middle of a write
    proc.kill()
    proc.wait()

    survivors = storage.load_jobs()
    assert survivors, "the download list must never be destroyed by a kill mid-save"
