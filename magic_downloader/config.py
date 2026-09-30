"""Application paths and default settings."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from magic_downloader.paths import DATA_DIR, DOWNLOADS_DIR, LEGACY_DOWNLOADS_DIRS, RESOURCE_ROOT, migrated_path

# Read-only resource root (project folder when run from source; bundle when frozen)
ROOT = RESOURCE_ROOT
SETTINGS_PATH = DATA_DIR / "settings.json"
JOBS_PATH = DATA_DIR / "jobs.json"

DEFAULT_SETTINGS: dict[str, Any] = {
    "default_save_path": str(DOWNLOADS_DIR),
    "connections": 8,
    "max_simultaneous": 3,
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "chunk_size": 256 * 1024,
    # Parallel workers used to fetch HLS/DASH segments (falls back to connections)
    "media_workers": 8,
    # Network behaviour
    "timeout": 60,             # per-request seconds
    "retries": 3,              # segment/stream retry attempts
    "max_speed_kbps": 0,       # global download cap in KB/s (0 = unlimited)
    # Video / ffmpeg
    "ffmpeg_path": "",         # explicit ffmpeg path; empty = auto-detect
    "default_video_quality": "ask",  # ask | best | 2160 | 1440 | 1080 | 720 | 480 | 360 | audio
    "stream_output_ts": False,  # save HLS/DASH video as a raw .ts stream instead of a merged .mp4 (needs ffmpeg)
    "prefer_smaller_files": False,  # bias yt-dlp to the smallest encode at a given resolution (efficient codecs / leaner bitrate)
    # UX
    "confirm_delete": True,
    # Download-list columns to show (right-click the list header to choose).
    # Empty means every column, so ones added by a later version show up for
    # anyone who hasn't picked their own set.
    "visible_columns": [],
    # Left-to-right order of the columns (drag a heading to move one), and their
    # pixel widths. Empty = the built-in layout.
    "column_order": [],
    "column_widths": {},
    # The version currently installed and when it was first launched, so the app
    # can show "updated <date>". Set the first time a new version runs.
    "installed_version": "",
    "updated_at": 0.0,
    # Keep running in the system tray when the window is closed;
    # only "Exit" actually quits.
    "close_to_tray": True,
    "minimize_to_tray": False,  # also hide to tray on the minimize button
    "last_save_dir": "",        # remember the folder the user last downloaded to
    # The detail window opens from a task row or its button; never automatically.
    "progress_close_on_complete": True,
    "category_paths": {
        "General": str(DOWNLOADS_DIR / "General"),
        "Compressed": str(DOWNLOADS_DIR / "Compressed"),
        "Documents": str(DOWNLOADS_DIR / "Documents"),
        "Music": str(DOWNLOADS_DIR / "Music"),
        "Video": str(DOWNLOADS_DIR / "Video"),
    },
    # Extensions that map a downloaded file to a category ("File Types").
    "category_extensions": {
        "Compressed": [".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"],
        "Documents": [".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf", ".odt"],
        "Music": [".mp3", ".flac", ".wav", ".aac", ".ogg", ".m4a", ".wma", ".opus"],
        "Video": [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".flv", ".ts"],
    },
    # Browser integration (extension talks to this localhost API)
    "browser_integration": True,
    "browser_port": 7374,
    "browser_token": "",  # optional shared secret; leave empty for local-only trust
    "browser_auto_start": True,  # start download immediately when captured from browser
    # Show the "Download File Info" dialog (name/category/folder) for
    # downloads captured from the browser, instead of starting them silently.
    "confirm_browser_captures": False,
    # Domain-to-folder rules take precedence over file-type categories.
    # Each item is {"domain": "example.com", "folder": "D:/Downloads/Example"}.
    "site_folder_rules": [],
}


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON so an interrupted write can never destroy the existing file.

    ``open(path, "w")`` truncates to zero bytes *before* anything is written, so
    a process killed mid-write — which is exactly what a Windows restart does to
    a running app — left a zero-byte or half-written file behind. The next launch
    then failed to parse it and came up with an empty download list.

    Instead: write a temp file, flush it to disk, keep the previous good copy as
    ``.bak``, then ``os.replace()`` — an atomic rename, so *path* is only ever
    the old complete file or the new complete file, never a partial one.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())          # survive a power cut, not just a kill
    if path.exists():
        try:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        except OSError:
            pass                      # a missing backup must not block the save
    # On Windows the rename fails with WinError 32 if anything holds the target
    # open even momentarily — antivirus, the search indexer, a backup agent. The
    # old file is still intact at that point, so retry briefly rather than let
    # the error escape into whatever thread happened to be saving.
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def read_json_with_recovery(path: Path, default: Any = None) -> Any:
    """Load JSON, falling back to the ``.bak`` left by :func:`atomic_write_json`.

    A corrupt main file is preserved as ``.corrupt`` rather than silently
    discarded, so a bad file can be inspected instead of vanishing.
    """
    backup = path.with_suffix(path.suffix + ".bak")
    for candidate in (path, backup):
        try:
            if not candidate.exists() or candidate.stat().st_size == 0:
                continue
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            if candidate is path:
                try:
                    shutil.copy2(path, path.with_suffix(path.suffix + ".corrupt"))
                except OSError:
                    pass
            continue
        if data is not None:
            return data
    return default


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    for path in DEFAULT_SETTINGS["category_paths"].values():
        Path(path).mkdir(parents=True, exist_ok=True)


def load_settings() -> dict[str, Any]:
    ensure_dirs()
    data = read_json_with_recovery(SETTINGS_PATH)
    if data is None:                       # no file, and no usable backup
        save_settings(DEFAULT_SETTINGS.copy())
        return DEFAULT_SETTINGS.copy()
    if not isinstance(data, dict):         # someone hand-edited it into nonsense
        data = {}
    merged = DEFAULT_SETTINGS.copy()
    merged.update(data)
    migrated_defaults = False
    for key in ("default_save_path", "last_save_dir"):
        if merged.get(key) and Path(merged[key]).expanduser() in LEGACY_DOWNLOADS_DIRS:
            merged[key] = str(DOWNLOADS_DIR)
            migrated_defaults = True
    # Existing 0.9 installs saved the previous defaults explicitly. Apply the
    # new browser workflow once; the user can still change either option later.
    if data.get("installed_version") == "0.9":
        if data.get("confirm_browser_captures") is True:
            merged["confirm_browser_captures"] = False
        if data.get("progress_close_on_complete") is False:
            merged["progress_close_on_complete"] = True
    # Deep-merge nested dicts so new default categories survive upgrades.
    cats = DEFAULT_SETTINGS["category_paths"].copy()
    cats.update(data.get("category_paths") or {})
    # Older installs kept the original category folders after the default
    # folder was changed. Move only untouched built-in paths to the new default.
    chosen = str(merged.get("default_save_path") or "")
    moved_categories = False
    for category, folder in cats.items():
        if any(Path(folder).expanduser() == root / category for root in LEGACY_DOWNLOADS_DIRS):
            cats[category] = str(DOWNLOADS_DIR / category) if Path(chosen) == DOWNLOADS_DIR else chosen
            moved_categories = True
        elif any(Path(folder).expanduser() == root for root in LEGACY_DOWNLOADS_DIRS):
            cats[category] = chosen
            moved_categories = True
    if chosen and Path(chosen) != DOWNLOADS_DIR:
        old_roots = (DOWNLOADS_DIR, *LEGACY_DOWNLOADS_DIRS)
        for category in DEFAULT_SETTINGS["category_paths"]:
            if any(Path(cats.get(category, "")) == root / category for root in old_roots):
                cats[category] = chosen
                moved_categories = True
    merged["category_paths"] = cats
    exts = {k: list(v) for k, v in DEFAULT_SETTINGS["category_extensions"].items()}
    exts.update(data.get("category_extensions") or {})
    merged["category_extensions"] = exts
    if moved_categories or migrated_defaults:
        try:
            save_settings(merged)
        except OSError:
            pass
    return merged


def save_settings(settings: dict[str, Any]) -> None:
    ensure_dirs()
    atomic_write_json(SETTINGS_PATH, settings)


def category_for_filename(filename: str, settings: dict[str, Any] | None = None) -> str:
    ext = Path(filename).suffix.lower()
    # Prefer the user's editable File Types mapping when available.
    if settings and isinstance(settings.get("category_extensions"), dict):
        for cat, exts in settings["category_extensions"].items():
            if ext in {str(e).lower() for e in exts}:
                return cat
        return "General"
    if ext in {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"}:
        return "Compressed"
    if ext in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf", ".odt"}:
        return "Documents"
    if ext in {".mp3", ".flac", ".wav", ".aac", ".ogg", ".m4a", ".wma", ".opus"}:
        return "Music"
    if ext in {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".flv", ".ts"}:
        return "Video"
    return "General"


def default_download_dir(settings: dict[str, Any]) -> Path:
    """The configured download root, shared by all fallback entrances."""
    return migrated_path(Path(settings.get("default_save_path") or DOWNLOADS_DIR).expanduser())


def resolve_save_path(settings: dict[str, Any], filename: str, category: str | None = None) -> Path:
    cat = category or category_for_filename(filename)
    base = settings.get("category_paths", {}).get(cat) or default_download_dir(settings)
    path = migrated_path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path / filename


def site_folder_for_url(settings: dict[str, Any], url: str) -> Path | None:
    """Return the configured folder for a page/download URL, if it matches."""
    host = (urlsplit(str(url or "")).hostname or "").lower().strip(".")
    if not host:
        return None
    for rule in settings.get("site_folder_rules") or []:
        if not isinstance(rule, dict):
            continue
        raw = str(rule.get("domain") or "").lower().strip().lstrip("*.")
        domain = (urlsplit(raw if "://" in raw else "https://" + raw).hostname or "").strip(".")
        folder = str(rule.get("folder") or "").strip()
        if domain and folder and (host == domain or host.endswith("." + domain)):
            return migrated_path(folder)
    return None
