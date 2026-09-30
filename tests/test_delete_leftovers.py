"""Removing a job must not orphan gigabytes — nor silently destroy them.

delete_job used to remove the multi-GB segment folder unconditionally but only
delete the .part when delete_files=True, which the default menu action never
passes. An aborted 4 GB download therefore stayed in the user's Downloads folder
invisibly forever, and — because every collision check treats a bare .part as an
occupied name — permanently pushed later downloads of the same file to
"name (1).ext".

The opposite mistake matters just as much: deleting a paused multi-GB .part on a
bare Delete keypress with no warning. Hence leftover_bytes(), so the UI can say
what is at stake, and delete_partial, so the user's answer is honoured.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from magic_downloader.manager import DownloadManager
from magic_downloader.models import DownloadStatus


@pytest.fixture
def mgr_with_job(data_dir, make_job, tmp_path):
    mgr = DownloadManager()
    job = make_job(filename="big.iso")
    job.save_path = str(tmp_path / "big.iso")
    job.status = DownloadStatus.PAUSED
    mgr.jobs.append(job)

    part = Path(job.save_path + ".part")
    part.write_bytes(b"x" * 4096)
    seg_dir = Path(job.save_path + f".mdtmp-{job.id}")
    (seg_dir / "video").mkdir(parents=True)
    (seg_dir / "video" / "000001.seg").write_bytes(b"y" * 2048)
    return mgr, job, part, seg_dir


def test_leftover_bytes_reports_part_and_segments(mgr_with_job):
    mgr, job, _part, _seg = mgr_with_job

    assert mgr.leftover_bytes([job.id]) == 4096 + 2048


def test_leftover_bytes_is_zero_when_nothing_is_on_disk(data_dir, make_job, tmp_path):
    mgr = DownloadManager()
    job = make_job(filename="clean.bin")
    job.save_path = str(tmp_path / "clean.bin")
    mgr.jobs.append(job)

    assert mgr.leftover_bytes([job.id]) == 0


def test_delete_removes_the_orphaned_part_by_default(mgr_with_job):
    """The reported bug: the .part outlived the job that owned it."""
    mgr, job, part, seg_dir = mgr_with_job

    mgr.delete_job(job.id)

    assert not part.exists(), "the .part must not be orphaned"
    assert not seg_dir.exists()
    assert mgr.get_job(job.id) is None


def test_delete_can_keep_the_partial_data_when_asked(mgr_with_job):
    """The user chose "keep" — hours of transfer must survive."""
    mgr, job, part, seg_dir = mgr_with_job

    mgr.delete_job(job.id, delete_partial=False)

    assert part.exists(), "keeping the data was explicitly requested"
    assert seg_dir.exists()
    assert mgr.get_job(job.id) is None


def test_delete_files_still_removes_the_finished_file(mgr_with_job):
    mgr, job, part, _seg = mgr_with_job
    done = Path(job.save_path)
    done.write_bytes(b"finished")

    mgr.delete_job(job.id, delete_files=True)

    assert not done.exists()
    assert not part.exists()


def test_delete_without_delete_files_keeps_the_finished_file(mgr_with_job):
    """Removing a completed row must never delete the download itself."""
    mgr, job, _part, _seg = mgr_with_job
    done = Path(job.save_path)
    done.write_bytes(b"finished")

    mgr.delete_job(job.id)

    assert done.exists(), "only the unfinished data may go"


def test_an_orphaned_part_no_longer_misnames_the_next_download(mgr_with_job):
    """A stray .part makes _dedupe treat the name as taken, forever."""
    from magic_downloader.engine import _dedupe

    mgr, job, part, _seg = mgr_with_job
    target = Path(job.save_path)

    assert _dedupe(target).name == "big (1).iso", "the .part occupies the name"

    mgr.delete_job(job.id)

    assert _dedupe(target).name == "big.iso", (
        "after cleanup the original name must be free again"
    )


def test_deleting_a_missing_job_is_harmless(data_dir):
    mgr = DownloadManager()
    mgr.delete_job("no-such-id")          # must not raise
