"""Multi-segment HTTP download engine."""

from __future__ import annotations

import mimetypes
import os
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from magic_downloader.paths import migrated_path
from typing import Callable
from urllib.parse import unquote, urlparse, urlsplit, urlunsplit, parse_qsl, urlencode

import requests

from magic_downloader.models import DownloadJob, DownloadStatus, SegmentState

ProgressCallback = Callable[[DownloadJob], None]
CancelCheck = Callable[[], bool]


class DownloadEngine:
    """Downloads a single job with multiple parallel connections."""

    def __init__(
        self,
        job: DownloadJob,
        user_agent: str,
        chunk_size: int = 256 * 1024,
        on_progress: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
        pause_event: threading.Event | None = None,
        rate_limiter=None,
        timeout: int = 60,
    ) -> None:
        self.job = job
        self.user_agent = user_agent
        self.chunk_size = chunk_size
        self.on_progress = on_progress
        self.cancel_check = cancel_check or (lambda: False)
        self.rate_limiter = rate_limiter
        self.timeout = max(5, int(timeout or 60))
        self.pause_event = pause_event or threading.Event()
        self.pause_event.set()  # set = running; clear = paused
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent})
        # Browser-captured session cookies / referrer
        if job.cookie:
            self._session.headers["Cookie"] = job.cookie
        if job.referrer:
            self._session.headers["Referer"] = job.referrer
        if job.extra_headers:
            for k, v in job.extra_headers.items():
                if k and v:
                    self._session.headers[str(k)] = str(v)
        self._speed_window: deque[tuple[float, int]] = deque()
        self._speed_total = 0
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def pause(self) -> None:
        self.pause_event.clear()

    def resume(self) -> None:
        self.pause_event.set()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"User-Agent": self.user_agent}
        if self.job.cookie:
            h["Cookie"] = self.job.cookie
        if self.job.referrer:
            h["Referer"] = self.job.referrer
        if self.job.extra_headers:
            h.update({str(k): str(v) for k, v in self.job.extra_headers.items() if k and v})
        if extra:
            h.update(extra)
        return h

    def _emit(self) -> None:
        if self.on_progress:
            self.on_progress(self.job)

    def _wait_if_paused(self) -> bool:
        """Return False if cancelled while waiting."""
        while not self.pause_event.is_set():
            if self._stop or self.cancel_check():
                return False
            time.sleep(0.15)
        return not (self._stop or self.cancel_check())

    def _account(self, n_bytes: int) -> None:
        """Record throughput for the speed readout and apply the global cap."""
        self._update_speed(n_bytes)
        if self.rate_limiter is not None:
            self.rate_limiter.throttle(n_bytes)

    def _update_speed(self, n_bytes: int) -> None:
        now = time.monotonic()
        with self._lock:
            self.job.tick_active()
            self._speed_window.append((now, n_bytes))
            self._speed_total += n_bytes
            # Expire old samples from the FRONT, keeping a running total (~3s of
            # samples). The old code rebuilt AND re-summed the whole window on
            # EVERY chunk of EVERY connection — hundreds of thousands of list ops
            # per second on a fast link, holding this lock the whole time, which
            # starved slower CPUs and made the app look hung.
            cutoff = now - 3.0
            while self._speed_window and self._speed_window[0][0] < cutoff:
                self._speed_total -= self._speed_window.popleft()[1]
            if self._speed_window:
                dt = max(0.001, now - self._speed_window[0][0])
                self.job.speed_bps = self._speed_total / dt

    def probe(self) -> None:
        """HEAD/GET to learn size, filename, range support."""
        self.job.status = DownloadStatus.CONNECTING
        self._emit()

        url = self.job.url
        u = urlsplit(url)
        if (u.hostname or "").endswith((".cdninstagram.com", ".fbcdn.net")):
            # Browser playback slices are not complete downloads.
            url = urlunsplit(u._replace(query=urlencode([(k,v) for k,v in parse_qsl(u.query, keep_blank_values=True)
                                                        if k.lower() not in ("bytestart", "byteend")])))
            self.job.url = url
        filename = self.job.filename

        # Some media CDNs reject HEAD and plain GET but accept byte ranges.
        verified_range = False
        try:
            r = self._session.head(url, allow_redirects=True, timeout=30)
            if r.status_code >= 400 or not r.headers.get("Content-Length"):
                r.close()
                r = self._session.get(url, headers=self._headers({"Range": "bytes=0-0"}),
                                      stream=True, allow_redirects=True, timeout=30)
        except requests.RequestException:
            r = self._session.get(url, headers=self._headers({"Range": "bytes=0-0"}),
                                  stream=True, allow_redirects=True, timeout=30)
        if r.status_code >= 400:
            r.close()
            r = self._session.get(url, stream=True, allow_redirects=True, timeout=30)
        if r.status_code == 403 and not self.job.media_meta.get("youtube_n_resolved"):
            from magic_downloader.media.youtube_browser import browser_video_id, resolve_browser_url
            if browser_video_id(url, self.job.referrer):
                r.close()
                url = resolve_browser_url(url, self.job.referrer, self.job.media_meta.get("browser_player_url", ""))
                self.job.url = url
                self.job.media_meta["youtube_n_resolved"] = True
                r = self._session.get(url, headers=self._headers({"Range": "bytes=0-0"}),
                                      stream=True, allow_redirects=True, timeout=30)
        if r.status_code >= 400:
            r.close()
            raise RuntimeError(f"HTTP {r.status_code}")

        final_url = str(r.url)
        self.job.url = final_url

        content_range = re.match(r"bytes\s+\d+-\d+/(\d+)", r.headers.get("Content-Range", ""))
        cl = r.headers.get("Content-Length")
        if r.status_code == 206 and content_range:
            self.job.total_size = int(content_range.group(1))
            verified_range = True
        elif cl and cl.isdigit():
            self.job.total_size = int(cl)

        accept = (r.headers.get("Accept-Ranges") or "").lower()
        # Some servers omit Accept-Ranges but still honor Range
        self.job.supports_ranges = verified_range or accept != "none"

        self.job.etag = r.headers.get("ETag") or ""
        self.job.last_modified = r.headers.get("Last-Modified") or ""

        cd = r.headers.get("Content-Disposition") or ""
        content_type = r.headers.get("Content-Type") or ""
        r.close()

        # The server sent a WEB PAGE, not the file: a sign-in wall, a consent or
        # bot-check interstitial, or a "viewer" link. Saving it produces a small
        # useless .html the user only discovers by opening it — and the download
        # is reported as successful. Fail with the actual fix instead. Genuine
        # HTML downloads (an .html target, or an explicit attachment) pass through.
        if _is_html_response(content_type) and not _wants_html(cd, final_url):
            raise RuntimeError(
                "网站返回了网页而非视频文件。请确认浏览器中能正常播放，"
                "然后从播放器旁的下载按钮重新选择资源。"
            )

        # Fix junk names (GUIDs / no extension) using the server's real name.
        new_name = _resolve_name(filename, cd, content_type, final_url)
        if new_name and new_name != filename:
            filename = _sanitize_name(new_name)
            self.job.filename = filename
            parent = migrated_path(self.job.save_path).parent
            self.job.save_path = str(_dedupe(parent / filename))

        # Verify range support with a tiny range request when size known
        if self.job.total_size > 1 and not verified_range:
            try:
                tr = self._session.get(
                    self.job.url,
                    headers=self._headers({"Range": "bytes=0-0"}),
                    timeout=20,
                    stream=True,
                )
                # What the server DOES beats what it advertises. Previously a
                # 200 here was only believed when nothing was advertised, so a
                # server sending "Accept-Ranges: bytes" and then ignoring Range
                # kept supports_ranges=True — and every parallel connection went
                # on to fetch the WHOLE file and write it at its own offset,
                # producing an oversized, corrupt file that still reported
                # Complete. A 200 to "bytes=0-0" means ranges are not honoured.
                if tr.status_code == 206:
                    self.job.supports_ranges = True
                elif tr.status_code == 200:
                    self.job.supports_ranges = False
                tr.close()
            except requests.RequestException:
                pass

        self._emit()

    def run(self) -> None:
        try:
            if self.job.total_size <= 0 or not self.job.segments:
                self.probe()
            if self._stop or self.cancel_check():
                self.job.status = DownloadStatus.CANCELLED
                self._emit()
                return
            # Pausing during connect/probe has to survive. probe() sets
            # CONNECTING and we would immediately overwrite it with DOWNLOADING,
            # while the workers block inside _wait_if_paused() and never reach
            # their own PAUSED check — so the row read "Downloading" at 0 B/s
            # forever and, because DOWNLOADING counts as busy, permanently held
            # one of the max_simultaneous slots. Returning here lets the runner
            # free the slot and re-queue the next download.
            if not self.pause_event.is_set():
                self.job.status = DownloadStatus.PAUSED
                self.job.speed_bps = 0.0
                self._emit()
                return

            self.job.status = DownloadStatus.DOWNLOADING
            if self.job.started_at is None:
                self.job.started_at = time.time()
            # Start the Avg-speed clock here, so the wait for the first bytes
            # counts as download time. Without this the first chunk has nothing
            # to measure against, and a file that arrives in one chunk would
            # record no time at all.
            self.job.tick_active()
            self._emit()

            migrated_path(self.job.save_path).parent.mkdir(parents=True, exist_ok=True)

            if (
                self.job.supports_ranges
                and self.job.total_size > 0
                and self.job.connections > 1
            ):
                self._run_multipart()
            else:
                self._run_single()

            if self._stop or self.cancel_check():
                if self.job.status != DownloadStatus.PAUSED:
                    self.job.status = DownloadStatus.CANCELLED
            elif self.job.status == DownloadStatus.PAUSED:
                pass
            elif self.job.total_size > 0 and self.job.downloaded >= self.job.total_size:
                self._finalize()
            elif self.job.total_size == 0 and self.job.downloaded > 0:
                # Unknown size single-stream completed
                self._finalize()
            elif self.job.status == DownloadStatus.DOWNLOADING:
                # Partial without pause flag → treat as failed if incomplete
                if self.job.total_size > 0 and self.job.downloaded < self.job.total_size:
                    self.job.status = DownloadStatus.FAILED
                    self.job.error = "下载不完整"
                else:
                    self._finalize()

            self.job.speed_bps = 0.0
            self._emit()
        except Exception as exc:  # noqa: BLE001 — surface to UI
            self.job.status = DownloadStatus.FAILED
            self.job.error = str(exc)
            self.job.speed_bps = 0.0
            self._emit()

    def _finalize(self) -> None:
        part = migrated_path(self.job.save_path + ".part")
        final = migrated_path(self.job.save_path)
        if part.exists():
            if final.exists():
                final.unlink()
            part.rename(final)
        self.job.status = DownloadStatus.COMPLETE
        self.job.finished_at = time.time()
        self.job.error = ""
        if self.job.total_size <= 0 and final.exists():
            self.job.total_size = final.stat().st_size
            self.job.downloaded = self.job.total_size

    def _ensure_part_file(self, size: int) -> Path:
        part = migrated_path(self.job.save_path + ".part")
        if not part.exists() or part.stat().st_size != size:
            # Pre-allocate sparse-ish empty file
            with open(part, "wb") as f:
                if size > 0:
                    f.truncate(size)
        return part

    def _build_segments(self) -> list[SegmentState]:
        if self.job.segments and sum(s.downloaded for s in self.job.segments) > 0:
            return self.job.segments

        n = max(1, min(self.job.connections, 32))
        total = self.job.total_size
        if total <= 0:
            return []

        # Prefer fewer connections for small files
        if total < 2 * 1024 * 1024:
            n = 1
        elif total < 10 * 1024 * 1024:
            n = min(n, 4)

        part = total // n
        segments: list[SegmentState] = []
        for i in range(n):
            start = i * part
            end = total - 1 if i == n - 1 else (start + part - 1)
            segments.append(SegmentState(index=i, start=start, end=end, downloaded=0))
        self.job.segments = segments
        return segments

    def _run_multipart(self) -> None:
        segments = self._build_segments()
        part_path = self._ensure_part_file(self.job.total_size)
        self.job.downloaded = sum(s.downloaded for s in segments)
        self._emit()

        pending = [s for s in segments if s.remaining > 0]
        if not pending:
            return

        workers = min(len(pending), self.job.connections)

        def worker(seg: SegmentState) -> None:
            self._download_segment(part_path, seg)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(worker, s) for s in pending]
            for fut in as_completed(futures):
                fut.result()  # re-raise worker errors

        self.job.downloaded = sum(s.downloaded for s in self.job.segments)

    def _download_segment(self, part_path: Path, seg: SegmentState) -> None:
        failures = 0
        while seg.remaining > 0:
            if not self._wait_if_paused():
                self.job.status = DownloadStatus.CANCELLED
                return
            if self.job.status == DownloadStatus.PAUSED:
                return

            current = seg.start + seg.downloaded
            end = seg.end
            headers = self._headers({"Range": f"bytes={current}-{end}"})
            try:
                with self._session.get(
                    self.job.url,
                    headers=headers,
                    stream=True,
                    timeout=self.timeout,
                ) as resp:
                    if resp.status_code not in (200, 206):
                        raise RuntimeError(f"HTTP {resp.status_code} for segment {seg.index}")
                    # A 200 to a ranged request means the body is the WHOLE file,
                    # not the slice we asked for. Writing it at this segment's
                    # offset would corrupt the file (and overrun into the next
                    # segment's region). Harmless only when this segment IS the
                    # whole file, which is the single-segment case.
                    if resp.status_code == 200 and (seg.start > 0 or len(self.job.segments) > 1):
                        raise RuntimeError(
                            "the server ignored the requested byte range "
                            f"(segment {seg.index}) — retry this download and it "
                            "will use a single connection"
                        )
                    with open(part_path, "r+b") as f:
                        f.seek(current)
                        for chunk in resp.iter_content(chunk_size=self.chunk_size):
                            if not chunk:
                                continue
                            if not self._wait_if_paused():
                                self.job.status = DownloadStatus.CANCELLED
                                return
                            if self.job.status == DownloadStatus.PAUSED:
                                return
                            f.write(chunk)
                            n = len(chunk)
                            with self._lock:
                                seg.downloaded += n
                                self.job.downloaded = sum(s.downloaded for s in self.job.segments)
                            self._account(n)
                            self._emit()
            except requests.RequestException as exc:
                # Resume from bytes actually written; reuse the same range checks.
                failures += 1
                if failures >= 4:
                    raise RuntimeError(f"Segment {seg.index} failed after 4 attempts") from exc
                time.sleep(1.0)
                if self._stop or self.cancel_check():
                    return

    def _run_single(self) -> None:
        part_path = migrated_path(self.job.save_path + ".part")
        mode = "ab" if part_path.exists() and self.job.downloaded > 0 else "wb"
        headers = self._headers()
        if mode == "ab" and self.job.downloaded > 0 and self.job.supports_ranges:
            headers["Range"] = f"bytes={self.job.downloaded}-"

        with self._session.get(self.job.url, headers=headers, stream=True, timeout=self.timeout) as resp:
            if resp.status_code == 200 and mode == "ab":
                # Server ignored resume — restart
                mode = "wb"
                self.job.downloaded = 0
            elif resp.status_code not in (200, 206):
                raise RuntimeError(f"HTTP {resp.status_code}")

            cl = resp.headers.get("Content-Length")
            if self.job.total_size <= 0 and cl and cl.isdigit():
                if resp.status_code == 206:
                    # Content-Length is remaining; try parse Content-Range
                    cr = resp.headers.get("Content-Range", "")
                    m = re.match(r"bytes\s+\d+-\d+/(\d+)", cr)
                    if m:
                        self.job.total_size = int(m.group(1))
                else:
                    self.job.total_size = int(cl)

            with open(part_path, mode) as f:
                for chunk in resp.iter_content(chunk_size=self.chunk_size):
                    if not chunk:
                        continue
                    if not self._wait_if_paused():
                        self.job.status = DownloadStatus.CANCELLED
                        return
                    if self.job.status == DownloadStatus.PAUSED:
                        return
                    f.write(chunk)
                    n = len(chunk)
                    with self._lock:
                        self.job.downloaded += n
                    self._account(n)
                    self._emit()


def _filename_from_url(url: str) -> str:
    path = urlparse(url).path
    name = unquote(path.rstrip("/").split("/")[-1] if path else "")
    if not name or name in (".", ".."):
        return "download"
    # Strip query-like junk sometimes left in path
    name = name.split("?")[0]
    return name or "download"


def _filename_from_content_disposition(cd: str) -> str | None:
    if not cd:
        return None
    # filename*=UTF-8''...
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", cd, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.I)
    if m:
        return m.group(1)
    m = re.search(r"filename\s*=\s*([^;]+)", cd, re.I)
    if m:
        return m.group(1).strip().strip('"')
    return None


def suggest_filename(url: str) -> str:
    return _filename_from_url(url)


#: Longest extension still treated as a real one (".AppImage" is 9 characters).
_MAX_EXT_LEN = 12
_MAX_NAME_LEN = 200

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{16,}$")


def looks_like_junk_name(name: str) -> bool:
    """True if a filename is unusable — no extension, or a GUID/hash blob like
    'a5de8a50-7e22-4163-97a4-914d6644caa7'."""
    if not name or name.strip().lower() in ("", "download", "video", "file"):
        return True
    stem, ext = os.path.splitext(name)
    # Real extensions run longer than five characters — .torrent, .AppImage,
    # .markdown — and rejecting those replaced a perfectly good filename with
    # whatever the server happened to send. The alphanumeric test still throws
    # out version-number tails like "release-v1.2.3-final".
    if not ext or len(ext) > _MAX_EXT_LEN or not ext[1:].isalnum():
        return True
    if _UUID_RE.match(stem) or _HEX_RE.match(stem):
        return True
    return False


_HTML_CTS = ("text/html", "application/xhtml+xml")
_HTML_EXTS = (".html", ".htm", ".xhtml", ".mhtml")


def _is_html_response(content_type: str) -> bool:
    return (content_type or "").split(";")[0].strip().lower() in _HTML_CTS


def _wants_html(cd: str, url: str) -> bool:
    """True when an HTML document is genuinely what was asked for.

    Judged from the SERVER's disposition and the URL the user actually clicked —
    deliberately never from the local filename, because that name may itself have
    been derived from the HTML response. That is exactly how "identifier.html"
    slipped past this check: the name was renamed to .html first, so the guard
    then concluded a web page was what the user wanted.

    ``Content-Disposition: attachment`` is an explicit "this is a download", so
    it is trusted even when the content type looks wrong.
    """
    if "attachment" in (cd or "").lower():
        return True
    if os.path.splitext(_filename_from_content_disposition(cd) or "")[1].lower() in _HTML_EXTS:
        return True
    try:
        path = urlparse(url).path
    except ValueError:
        return False
    return os.path.splitext(path)[1].lower() in _HTML_EXTS


def _ext_from_content_type(ct: str) -> str:
    ct = (ct or "").split(";")[0].strip().lower()
    if not ct or ct in ("application/octet-stream", "binary/octet-stream", "application/download"):
        return ""
    # NEVER auto-name a download ".html" from the content type. A web page here
    # means a sign-in wall / interstitial, not the file — naming it "x.html"
    # produced a junk file AND made the HTML check below think the user actually
    # wanted a web page, so it waved the download through. Leaving the name junk
    # keeps that check honest.
    if ct in _HTML_CTS:
        return ""
    ext = mimetypes.guess_extension(ct)
    fixes = {".jpe": ".jpg", ".htm": ".html", ".mp4a": ".m4a"}
    return fixes.get(ext, ext) or ""


def _sanitize_name(name: str) -> str:
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    name = name.strip().strip(".") or "download"
    if len(name) <= _MAX_NAME_LEN:
        return name
    # Truncate the STEM, not the whole name: a blind name[:200] cut the
    # extension off a very long server-supplied filename, so the file was saved
    # with none at all and Windows had nothing to open it with.
    stem, ext = os.path.splitext(name)
    if ext and len(ext) <= _MAX_EXT_LEN and ext[1:].isalnum():
        return stem[: _MAX_NAME_LEN - len(ext)] + ext
    return name[:_MAX_NAME_LEN]


def _dedupe(path: Path) -> Path:
    if not path.exists() and not Path(str(path) + ".part").exists():
        return path
    stem, suf = path.stem, path.suffix
    i = 1
    while True:
        cand = path.with_name(f"{stem} ({i}){suf}")
        if not cand.exists() and not Path(str(cand) + ".part").exists():
            return cand
        i += 1


def _resolve_name(current: str, cd: str, content_type: str, final_url: str) -> str | None:
    """Best filename from the server, used only when ``current`` is junk."""
    cd_name = _filename_from_content_disposition(cd)
    if cd_name and looks_like_junk_name(current):
        return cd_name
    if not looks_like_junk_name(current):
        return None
    # current is junk and the server didn't name it — try the URL, then add an
    # extension inferred from the content type.
    base = _filename_from_url(final_url)
    if not looks_like_junk_name(base):
        return base
    ext = _ext_from_content_type(content_type)
    if ext:
        stem = os.path.splitext(current)[0] or os.path.splitext(base)[0] or "download"
        return stem + ext
    return None


def resolve_download_name(
    url: str,
    cookie: str = "",
    referrer: str = "",
    user_agent: str = "",
    timeout: int = 12,
) -> tuple[str, int]:
    """Ask the server (HEAD, falling back to GET) for the real filename + size.

    Returns (filename_or_empty, size). Used before showing the download dialog
    so junk URLs (GUIDs) get their real name/extension.
    """
    headers: dict[str, str] = {}
    if user_agent:
        headers["User-Agent"] = user_agent
    if cookie:
        headers["Cookie"] = cookie
    if referrer:
        headers["Referer"] = referrer
    try:
        r = requests.head(url, headers=headers, allow_redirects=True, timeout=timeout)
        if r.status_code >= 400 or not (
            r.headers.get("Content-Disposition") or r.headers.get("Content-Type")
        ):
            r = requests.get(url, headers=headers, allow_redirects=True, timeout=timeout, stream=True)
            r.close()
    except requests.RequestException:
        return "", 0

    final_url = str(r.url)
    cd = r.headers.get("Content-Disposition") or ""
    ct = r.headers.get("Content-Type") or ""
    size = 0
    cl = r.headers.get("Content-Length")
    if cl and str(cl).isdigit():
        size = int(cl)

    name = _filename_from_content_disposition(cd) or _filename_from_url(final_url)
    if looks_like_junk_name(name):
        ext = _ext_from_content_type(ct)
        if ext:
            stem = os.path.splitext(name)[0] or "download"
            name = stem + ext
    return (_sanitize_name(name) if name else ""), size
