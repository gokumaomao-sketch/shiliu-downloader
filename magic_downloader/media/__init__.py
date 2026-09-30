"""Media handling: streaming-video detection and HLS/DASH download support.

This package turns 拾流下载器 into a video grabber. The
browser extension sniffs media URLs from network traffic; this package knows
how to classify them (``detect``) and, for streaming manifests, download all
segments and mux them into a single playable file (via :mod:`ffmpeg`).
"""

from __future__ import annotations

from magic_downloader.media.detect import (
    MediaKind,
    classify_url,
    detect_kind,
    is_media_file,
)

__all__ = ["MediaKind", "classify_url", "detect_kind", "is_media_file"]
