"""本次迁移仅解析磁盘位置，不重写任务/配置；临时目录验证删除操作。"""
from pathlib import Path
from magic_downloader import paths
from magic_downloader.manager import DownloadManager
from magic_downloader.models import DownloadJob
from magic_downloader import config


def test_project_path_without_legacy_root_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, 'LEGACY_PROJECT_ROOT', None)
    custom = tmp_path / 'custom' / 'video.mp4'
    assert paths.migrated_path(custom) == custom


def test_migration_file_operations_preserve_records(monkeypatch, tmp_path):
    old, new = tmp_path / 'old', tmp_path / 'new'
    monkeypatch.setattr(paths, 'LEGACY_PROJECT_ROOT', old)
    monkeypatch.setattr(paths, 'MIGRATED_PROJECT_ROOT', new)
    original = str(old / 'archive' / 'video.mp4')
    resolved = new / 'archive' / 'video.mp4'
    resolved.parent.mkdir(parents=True)
    resolved.write_bytes(b'archive')
    assert paths.migrated_path(original) == resolved
    assert paths.migrated_path(tmp_path / 'custom' / 'video.mp4') == tmp_path / 'custom' / 'video.mp4'
    assert paths.migrated_path(Path(str(old) + '-other') / 'file') == Path(str(old) + '-other') / 'file'
    assert paths.migrated_path(old / '..' / 'custom') == old / '..' / 'custom'
    settings = {'default_save_path':str(old / 'archive'), 'category_paths': {'Video':str(old / 'archive')}}
    assert config.default_download_dir(settings) == resolved.parent
    assert config.resolve_save_path(settings, 'test.mp4', 'Video') == resolved.parent / 'test.mp4'
    assert settings['default_save_path'] == str(old / 'archive')
    job = DownloadJob(url='https://example.test/video', save_path=original, filename='video.mp4')
    manager = object.__new__(DownloadManager)
    import threading
    manager._lock = threading.RLock(); manager.jobs = [job]
    manager.cancel_job = lambda _: None
    manager._persist = lambda **_: None
    manager._notify = lambda: None
    part, scratch = manager._leftover_paths(job)
    part.write_bytes(b'part');scratch.mkdir();(scratch / 'segment').write_bytes(b'segment')
    assert manager.leftover_bytes([job.id]) == 11
    assert job.save_path == original
    manager.delete_job(job.id, delete_files=True)
    assert not resolved.exists() and not part.exists() and not scratch.exists()
    assert not old.exists()
