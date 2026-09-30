import json
from pathlib import Path

from magic_downloader import config


def test_old_category_paths_follow_changed_default(tmp_path, monkeypatch):
    legacy = Path.home() / "Downloads" / "MagicDownloaderCN"
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({
        "default_save_path": "U:/下载",
        "category_paths": {category: str(legacy / category)
                           for category in config.DEFAULT_SETTINGS["category_paths"]},
        "site_folder_rules": [{"domain": "example.com", "folder": "U:/单独归档"}],
    }), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_PATH", settings_file)
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)

    settings = config.load_settings()
    assert all(folder == "U:/下载" for folder in settings["category_paths"].values())
    assert config.site_folder_for_url(settings, "https://example.com/video") == Path("U:/单独归档")


def test_v21_default_migration_and_shared_fallbacks(tmp_path, monkeypatch):
    from magic_downloader.paths import LEGACY_DOWNLOADS_DIRS
    from magic_downloader.manager import DownloadManager
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    custom = str(tmp_path / "手动选择")
    independent = [{"domain": "example.com", "folder": custom}]
    for old in LEGACY_DOWNLOADS_DIRS[:2]:
        for chosen in (str(old), custom):
            payload = {"default_save_path": chosen, "last_save_dir": str(old),
                       "category_paths": {"Video": custom, "Music": str(old / "Music")},
                       "site_folder_rules": independent}
            config.SETTINGS_PATH.write_text(json.dumps(payload))
            result = config.load_settings()
            expected = str(config.DOWNLOADS_DIR) if chosen == str(old) else custom
            assert result["default_save_path"] == expected
            assert result["last_save_dir"] == str(config.DOWNLOADS_DIR)
            assert result["category_paths"]["Video"] == custom
            assert result["category_paths"]["Music"] == (str(config.DOWNLOADS_DIR / "Music")
                                                        if chosen == str(old) else custom)
            assert result["site_folder_rules"] == independent
            assert config.load_settings() == result  # migration persists once
            # Browser/site fallback and manual file destination share the same root.
            settings = {"default_save_path": expected, "category_paths": {}}
            manager = object.__new__(DownloadManager); manager.settings = settings
            route = manager.preview_capture_destination({"url": "https://none.test/f.bin"})
            assert route["folder"] == str(config.default_download_dir(settings))
            assert config.resolve_save_path(settings, "f.bin").parent == Path(route["folder"])
    config.SETTINGS_PATH.unlink()
    config.SETTINGS_PATH.with_suffix(".json.bak").unlink()
    fresh = config.load_settings()
    assert fresh["default_save_path"] == str(Path.home() / "Downloads" / "拾流下载器")
    assert config.default_download_dir({}) == config.DOWNLOADS_DIR
