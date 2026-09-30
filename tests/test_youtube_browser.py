"""Browser URL repair must preserve HTTP routing and reject untrusted scripts."""
from unittest.mock import Mock, patch
import pytest
from magic_downloader.engine import DownloadEngine
from magic_downloader.models import DownloadJob
from magic_downloader.media.youtube_browser import validate_player_url

def test_403_browser_url_repaired_and_probed(tmp_path):
    original = 'https://r1.googlevideo.com/videoplayback?n=raw'
    repaired = original.replace('raw', 'solved')
    job = DownloadJob(url=original, filename='video.mp4', save_path=str(tmp_path/'video.mp4'),
                      referrer='https://www.youtube.com/watch?v=DemoVideo01')
    engine = DownloadEngine(job, 'test')
    denied = Mock(status_code=403, headers={})
    ok = Mock(status_code=206, url=repaired, headers={'Content-Type':'video/mp4',
              'Content-Length':'1', 'Content-Range':'bytes 0-0/12345'})
    engine._session = Mock()
    engine._session.head.return_value = denied
    engine._session.get.side_effect = [denied, denied, ok]
    with patch('magic_downloader.media.youtube_browser.resolve_browser_url',return_value=repaired) as solve:
        engine.probe()
        solve.assert_called_once_with(original, job.referrer, '')
    assert job.url == repaired and job.total_size == 12345 and job.supports_ranges
    assert job.media_meta['youtube_n_resolved']
    engine._session.get.side_effect = None
    engine._session.get.return_value = denied
    with patch('magic_downloader.media.youtube_browser.resolve_browser_url') as solve:
        with pytest.raises(RuntimeError, match='HTTP 403'): engine.probe()
        solve.assert_not_called()
    for unsafe in ('http://www.youtube.com/s/player/x/en/base.js',
                   'https://evil.test/s/player/x/en/base.js',
                   'https://www.youtube.com@evil.test/s/player/x/en/base.js',
                   'https://www.youtube.com/s/player/x/../base.js'):
        with pytest.raises(ValueError): validate_player_url(unsafe)
