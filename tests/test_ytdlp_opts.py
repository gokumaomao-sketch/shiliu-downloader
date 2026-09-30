from magic_downloader.media.ytdlp_engine import _base_opts


def test_youtube_uses_supported_player_clients_with_or_without_cookies():
    url = "https://www.youtube.com/watch?v=DemoVideo01"
    opts = _base_opts(cookie="test=value", use_cookies=True, url=url)
    assert opts["extractor_args"] == {"youtube": {"player_client": ["default", "web_embedded"]}}
    assert _base_opts(url=url)["extractor_args"] == {"youtube": {"player_client": ["default", "web_embedded"]}}
    assert "extractor_args" not in _base_opts(cookie="test=value", url="https://www.bilibili.com/video/1")
