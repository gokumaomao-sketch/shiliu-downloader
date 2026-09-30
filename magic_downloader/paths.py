"""Filesystem locations that work both from source and as a frozen exe.

On macOS, writable data uses Application Support in both source and packaged
runs. When packaged with PyInstaller (``sys.frozen``):
  * read-only bundled files (the browser extension) come from the bundle;
  * writable data lives in macOS Application Support or the Windows user app
    data folder, so it works even if the app bundle is read-only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """Folder containing read-only bundled resources."""
    if is_frozen():
        if sys.platform == "darwin":
            # PyInstaller places app resources at Contents/Resources. Resolve
            # from the launcher path without following its internal symlinks.
            app_resources = Path(sys.executable).absolute().parent.parent / "Resources"
            if app_resources.is_dir():
                return app_resources
        base = getattr(sys, "_MEIPASS", None)
        return Path(base) if base else Path(sys.executable).resolve().parent
    # magic_downloader/paths.py -> project root
    return Path(__file__).resolve().parent.parent


def _migrate_data_root(root: Path, legacy: Path) -> None:
    # 新目录存在后不再读取旧目录，也不把旧目录作为回退源。
    if root.exists() or not legacy.exists():
        return
    import hashlib
    import json
    import shutil
    import tempfile

    def hashes(directory: Path) -> dict[str, str]:
        result = {}
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise RuntimeError(f"数据迁移不接受软链接：{path}")
            if path.is_file():
                result[str(path.relative_to(directory))] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    if legacy.is_symlink():
        raise RuntimeError(f"数据迁移不接受软链接：{legacy}")
    expected = hashes(legacy)
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".拾流迁移-", dir=root.parent) as temporary:
        staged = Path(temporary) / "verified"
        shutil.copytree(legacy, staged)
        if hashes(staged) != expected or hashes(legacy) != expected:
            raise RuntimeError("数据迁移校验失败或源数据正在变化，未切换目录")
        for filename, expected_type in (("jobs.json", list), ("settings.json", dict)):
            path = staged / "data" / filename
            if not isinstance(json.loads(path.read_text(encoding="utf-8")), expected_type):
                raise RuntimeError(f"数据迁移校验失败：{filename}")
        # 仅调整视频号组件内部证书路径，保留下载路径及用户配置。
        config = staged / "channels" / "config.yaml"
        if config.is_file():
            text = config.read_text(encoding="utf-8")
            updated = text.replace(str(legacy / "channels") + "/", str(root / "channels") + "/")
            if updated != text:
                config.write_text(updated, encoding="utf-8")
        staged.rename(root)


def data_root() -> Path:
    """Writable per-user app folder, shared by macOS source and packaged runs."""
    if sys.platform == "darwin":
        support = Path.home() / "Library" / "Application Support"
        root = support / "拾流下载器"
        _migrate_data_root(root, support / "DODO DownLoader")
        root.mkdir(parents=True, exist_ok=True)
        return root
    if is_frozen():
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        root = Path(base) / "MagicDownloaderCN"
    else:
        root = Path(__file__).resolve().parent.parent
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return root


RESOURCE_ROOT = resource_root()
DATA_ROOT = data_root()

DATA_DIR = DATA_ROOT / "data"          # settings.json, jobs.json
BIN_DIR = DATA_ROOT / "bin"            # auto-installed ffmpeg lives here
DOWNLOADS_DIR = Path.home() / "Downloads" / "拾流下载器"
# Historical names are only used to recognize untouched defaults.
LEGACY_DOWNLOADS_DIRS = tuple(Path.home() / "Downloads" / name
                            for name in ("DODO DownLoader", "DODO Downloader", "MagicDownloaderCN"))


def extension_dir() -> Path:
    """Where the loadable, unpacked extension lives (a stable, real folder)."""
    if is_frozen():
        if sys.platform == "darwin":
            return RESOURCE_ROOT / "browser_extension"
        return Path(sys.executable).resolve().parent / "browser_extension"
    return RESOURCE_ROOT / "browser_extension"


def install_txt_path() -> Path:
    if is_frozen():
        if sys.platform == "darwin":
            return RESOURCE_ROOT / "INSTALL_BROWSER.txt"
        return Path(sys.executable).resolve().parent / "INSTALL_BROWSER.txt"
    return RESOURCE_ROOT / "INSTALL_BROWSER.txt"




# 历史项目路径仅在明确提供旧根目录时解析，不改写任务或用户配置。
LEGACY_PROJECT_ROOT = (Path(os.environ["SHILIU_LEGACY_PROJECT_ROOT"]).expanduser()
                       if os.environ.get("SHILIU_LEGACY_PROJECT_ROOT") else None)
MIGRATED_PROJECT_ROOT = (Path(os.environ["SHILIU_PROJECT_ROOT"]).expanduser()
                         if os.environ.get("SHILIU_PROJECT_ROOT") else RESOURCE_ROOT)


def migrated_path(value: str | Path) -> Path:
    path = Path(value)
    if LEGACY_PROJECT_ROOT is None:
        return path
    try:
        relative = path.relative_to(LEGACY_PROJECT_ROOT)
    except ValueError:
        return path
    if ".." in relative.parts:
        return path
    return MIGRATED_PROJECT_ROOT / relative
