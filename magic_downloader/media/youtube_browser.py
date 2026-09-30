"""Resolve the player n challenge on browser-provided YouTube media URLs."""
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from magic_downloader.paths import BIN_DIR, RESOURCE_ROOT


def find_node() -> str | None:
    """Find Node in app resources, user app data, then the host environment."""
    name = "node.exe" if sys.platform == "win32" else "node"
    candidates = [RESOURCE_ROOT / "bin" / name]
    if sys.platform == "darwin":
        candidates.append(BIN_DIR / name)
        candidates.extend((Path("/opt/homebrew/bin/node"), Path("/usr/local/bin/node")))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("node")


def browser_video_id(url: str, referrer: str) -> str | None:
    try:
        media, page = urlsplit(url), urlsplit(referrer)
        if (media.scheme != "https" or not (media.hostname or "").endswith(".googlevideo.com")
                or media.username or media.password or media.port not in (None, 443)
                or media.path != "/videoplayback" or "n" not in parse_qs(media.query)
                or page.scheme != "https" or page.hostname not in ("youtube.com", "www.youtube.com", "m.youtube.com")):
            return None
        video_id = parse_qs(page.query).get("v", [""])[0] if page.path == "/watch" else page.path.removeprefix("/shorts/")
        return video_id if re.fullmatch(r"[\w-]{11}", video_id, flags=re.ASCII) else None
    except ValueError:
        return None


def validate_player_url(url: str) -> str:
    u = urlsplit(url)
    if (u.scheme != "https" or u.hostname != "www.youtube.com" or u.port not in (None, 443)
            or u.username or u.password or ".." in u.path
            or not re.fullmatch(r"/s/player/[\w-]+/[\w./-]+/base\.js", u.path, flags=re.ASCII)):
        raise ValueError("播放器脚本地址无效")
    return url


def resolve_browser_url(url: str, referrer: str, player_url: str = "") -> str:
    video_id = browser_video_id(url, referrer)
    if not video_id:
        raise ValueError("不是可处理的 YouTube 浏览器视频地址")
    from .ytdlp_engine import _base_opts, _import_ytdlp
    from yt_dlp.extractor.youtube import YoutubeIE
    from yt_dlp.extractor.youtube.jsc._director import initialize_jsc_director
    from yt_dlp.extractor.youtube.jsc.provider import JsChallengeRequest, JsChallengeType, NChallengeInput

    node = find_node()
    if not node:
        raise RuntimeError("缺少 YouTube 地址处理运行库，请使用完整的软件包")
    opts = _base_opts()
    opts.update(cachedir=False, js_runtimes={"node": {"path": node}})
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    challenge = query["n"][0]
    with _import_ytdlp().YoutubeDL(opts) as ydl:
        ie = YoutubeIE(ydl)
        if not player_url:
            webpage = ie._download_webpage(f"https://www.youtube.com/embed/{video_id}", video_id)
            match = re.search(r'/s/player/[A-Za-z0-9_-]+/[^"\s<>]*base\.js', webpage)
            if not match:
                raise RuntimeError("未取得 YouTube 播放器脚本，请从刷新后的网页重新发起下载")
            player_url = "https://www.youtube.com" + match[0]
        player_url = validate_player_url(player_url)
        director = initialize_jsc_director(ie)
        request = JsChallengeRequest(type=JsChallengeType.N, video_id=video_id,
            input=NChallengeInput(challenges=[challenge], player_url=player_url))
        for _, response in director.bulk_solve([request]):
            solved = response.output.results.get(challenge)
            if solved:
                query["n"] = [solved]
                return urlunsplit(parsed._replace(query=urlencode(query, doseq=True)))
    raise RuntimeError("YouTube 地址处理失败，请刷新视频页面后重新发起下载")
