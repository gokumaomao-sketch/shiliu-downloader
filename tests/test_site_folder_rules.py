from magic_downloader.config import site_folder_for_url
from magic_downloader.manager import DownloadManager


def test_site_rule_matches_domain_and_subdomain_only():
    settings = {"site_folder_rules": [{"domain": "douyin.com", "folder": "D:/素材/抖音"}]}
    assert site_folder_for_url(settings, "https://www.douyin.com/video/1").as_posix() == "D:/素材/抖音"
    assert site_folder_for_url(settings, "https://notdouyin.com/video/1") is None


def test_browser_capture_uses_page_domain_rule(tmp_path):
    manager = DownloadManager()
    manager.settings["site_folder_rules"] = [{"domain": "douyin.com", "folder": str(tmp_path)}]
    suggested = manager.suggest_capture({"url": "https://cdn.example.com/video.mp4", "page_url": "https://www.douyin.com/video/1"})
    assert suggested["folder"] == str(tmp_path)


def test_route_preview_matches_capture_without_creating_folder(tmp_path):
    manager = DownloadManager()
    target = tmp_path / "site"
    manager.settings["site_folder_rules"] = [{"domain": "91porn.com", "folder": str(target)}]
    data = {"url": "https://cdn.example/video.mp4", "page_url": "https://www.91porn.com/view_video.php?viewkey=abc",
            "filename": "video.mp4", "media_type": "http"}
    preview = manager.preview_capture_destination(data)
    assert preview["folder"] == manager.suggest_capture(data)["folder"] == str(target)
    assert preview["rule"] == "91porn.com"
    assert not target.exists()


def test_full_site_url_routes_cdn_download_by_page(tmp_path):
    manager = DownloadManager()
    manager.settings["site_folder_rules"] = [{"domain": "https://www.91porn.com/", "folder": str(tmp_path)}]
    suggested = manager.suggest_capture({
        "url": "https://la.btc620.com/mp43/124025.mp4?st=token",
        "page_url": "https://www.91porn.com/view_video.php?viewkey=abc",
        "filename": "video.mp4",
    })
    assert suggested["folder"] == str(tmp_path)
