"""Persist jobs and settings to JSON."""

from __future__ import annotations

from typing import Any

from magic_downloader.config import (
    JOBS_PATH,
    atomic_write_json,
    ensure_dirs,
    read_json_with_recovery,
)
from magic_downloader.models import DownloadJob


def load_jobs() -> list[DownloadJob]:
    ensure_dirs()
    # Recovers from the .bak if the main file was truncated by an unclean
    # shutdown — otherwise the whole download list silently came back empty.
    raw: list[dict[str, Any]] = read_json_with_recovery(JOBS_PATH, [])
    if not isinstance(raw, list):
        return []
    jobs: list[DownloadJob] = []
    for item in raw:
        try:
            jobs.append(DownloadJob.from_dict(item))
        except Exception:  # noqa: BLE001 — one bad row must not drop the rest
            continue
    return jobs


def save_jobs(jobs: list[DownloadJob]) -> None:
    save_jobs_payload([j.to_dict() for j in jobs])


def save_jobs_payload(payload: list[dict[str, Any]]) -> None:
    """Write already-serialized job dicts. Split from save_jobs so a caller can
    serialize under its state lock but do the (slow) disk write outside it."""
    ensure_dirs()
    atomic_write_json(JOBS_PATH, payload)
