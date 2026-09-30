import json
from magic_downloader import config

def test_upgrade_changes_old_browser_defaults(tmp_path, monkeypatch):
    p = tmp_path / 'settings.json'
    p.write_text(json.dumps({'installed_version':'0.9','confirm_browser_captures':True,
                             'progress_close_on_complete':False}),encoding='utf-8')
    monkeypatch.setattr(config,'SETTINGS_PATH',p)
    settings = config.load_settings()
    assert settings['confirm_browser_captures'] is False
    assert settings['progress_close_on_complete'] is True
    assert settings['browser_auto_start'] is True
