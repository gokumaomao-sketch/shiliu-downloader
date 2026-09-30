import threading
from magic_downloader.manager import DownloadManager
from magic_downloader.models import DownloadJob, DownloadStatus

def test_status_exposes_browser_progress(tmp_path):
    manager = object.__new__(DownloadManager)
    manager._lock = threading.RLock()
    job = DownloadJob(url='https://example.com/video.mp4',save_path=str(tmp_path/'video.mp4'),filename='video.mp4')
    job.status = DownloadStatus.DOWNLOADING
    job.total_size = 1000
    job.downloaded = 250
    job.speed_bps = 120
    manager.jobs = [job]
    state = manager.status_snapshot()
    assert state['jobs'][0] == {'id':job.id,'status':'Downloading','filename':'video.mp4',
                                'downloaded':250,'total_size':1000,'speed_bps':120,'progress':25.0}
    assert 'url' not in state['jobs'][0]
