"""Streaming-media stack: URL classification, HLS/DASH parsing, assembly.

Covers ``magic_downloader/media``:

* ``detect``      – .m3u8/.mpd/progressive classification, Content-Type wins over
                    the extension, query strings that hide the extension, and the
                    ``media_type`` hint the browser extension sends.
* ``hls``/``dash`` – manifests are parsed from text written inline below, so a
                    regression in the parsers shows up here and not as a corrupt
                    video three hours into a download.
* ``media_engine`` – planning (which tracks/segments to fetch), the streaming
                    concat that frees each segment as it is folded in (a 6.5 GB
                    video used to need 13 GB of temp space), and the .ts/.mp4
                    output choice including the fallback when a .ts mux fails.
* ``ffmpeg``      – executable lookup order and the argument/retry logic, with
                    the subprocess call stubbed. No real ffmpeg is ever invoked.

Nothing here touches the network: sessions are the conftest fakes serving the
inline manifests, and every ffmpeg call is a recorder.
"""

from __future__ import annotations

import builtins
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# ───────────────────────────── manifests ──────────────────────────────────

MASTER_URL = "https://cdn.test/vod/master.m3u8"
MID_URL = "https://cdn.test/vod/mid/index.m3u8"
AUDIO_URL = "https://cdn.test/vod/audio/en.m3u8"

MASTER_M3U8 = """#EXTM3U
#EXT-X-VERSION:4
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aac",NAME="English",LANGUAGE="en",DEFAULT=YES,URI="audio/en.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.42c01e,mp4a.40.2",AUDIO="aac"
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000,AVERAGE-BANDWIDTH=2500000,RESOLUTION=1920x1080,FRAME-RATE=29.970,CODECS="avc1.640028,mp4a.40.2",AUDIO="aac"
https://edge.cdn.test/high/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1500000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2",AUDIO="aac"
mid/index.m3u8
"""

MEDIA_M3U8 = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:5
#EXTINF:9.009,
seg0.ts
#EXTINF:9.009,
https://edge.cdn.test/mid/seg1.ts
#EXTINF:3.482,
../shared/seg2.ts
#EXT-X-ENDLIST
"""

AUDIO_M3U8 = """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXTINF:9.009,
a0.aac
#EXTINF:9.009,
a1.aac
#EXT-X-ENDLIST
"""

FMP4_M3U8 = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:6
#EXT-X-MAP:URI="init.mp4"
#EXTINF:6.0,
seg-1.m4s
#EXTINF:6.0,
seg-2.m4s
#EXT-X-ENDLIST
"""

AES_M3U8 = """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-KEY:METHOD=AES-128,URI="https://key.test/k1.key",IV=0x00000000000000000000000000000009
#EXTINF:10.0,
enc0.ts
#EXT-X-KEY:METHOD=NONE
#EXTINF:10.0,
clear1.ts
#EXT-X-ENDLIST
"""

BYTERANGE_M3U8 = """#EXTM3U
#EXT-X-VERSION:4
#EXT-X-TARGETDURATION:10
#EXTINF:9.0,
#EXT-X-BYTERANGE:1000@0
whole.ts
#EXTINF:9.0,
#EXT-X-BYTERANGE:2000
whole.ts
#EXT-X-ENDLIST
"""

MPD_URL = "https://cdn.test/dash/manifest.mpd"

MPD_NUMBER = """<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     profiles="urn:mpeg:dash:profile:isoff-live:2011"
     mediaPresentationDuration="PT1M0.0S" minBufferTime="PT2S">
  <Period id="0">
    <AdaptationSet mimeType="video/mp4" segmentAlignment="true">
      <SegmentTemplate initialization="$RepresentationID$/init.mp4"
                       media="$RepresentationID$/seg-$Number%03d$.m4s"
                       startNumber="1" timescale="1000" duration="20000"/>
      <Representation id="v720" codecs="avc1.4d401f" bandwidth="1200000" width="1280" height="720"/>
      <Representation id="v1080" codecs="avc1.640028" bandwidth="3000000" width="1920" height="1080"/>
    </AdaptationSet>
    <AdaptationSet mimeType="audio/mp4" lang="en">
      <Representation id="a128" codecs="mp4a.40.2" bandwidth="128000" audioSamplingRate="48000">
        <SegmentList>
          <Initialization sourceURL="audio/init.mp4"/>
          <SegmentURL media="audio/seg-001.m4s"/>
          <SegmentURL media="audio/seg-002.m4s"/>
          <SegmentURL media="audio/seg-003.m4s"/>
        </SegmentList>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""

MPD_TIMELINE = """<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT1H2M3.5S">
  <Period>
    <BaseURL>https://edge.test/v/</BaseURL>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v1" bandwidth="900000" width="854" height="480">
        <SegmentTemplate initialization="init.mp4" media="chunk-$Time$.m4s" timescale="90000">
          <SegmentTimeline>
            <S t="0" d="180000" r="2"/>
            <S d="90000"/>
          </SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


# ───────────────────────────── fixtures ───────────────────────────────────


@pytest.fixture
def url_session(fake_response, fake_session):
    """A conftest FakeSession that serves inline manifest text per URL."""

    class Resp(fake_response):
        def __init__(self, url, text, status_code=200, content_type="application/x-mpegURL"):
            super().__init__(
                url=url,
                body=text.encode("utf-8"),
                status_code=status_code,
                headers={"Content-Type": content_type},
            )
            self.text = text

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    class Session(fake_session):
        def __init__(self, pages):
            super().__init__()
            self.pages = pages

        def get(self, url, **kw):
            self.calls.append(("get", url, kw))
            if url not in self.pages:
                raise AssertionError(f"unexpected request for {url!r}")
            return Resp(url, self.pages[url])

    return Session


@pytest.fixture
def engine(tmp_path, make_job):
    """A MediaDownloadEngine whose job saves into tmp_path (no network used)."""
    from magic_downloader.media.detect import MediaKind
    from magic_downloader.media.media_engine import MediaDownloadEngine

    job = make_job(filename="out.mp4", url=MASTER_URL)
    job.save_path = str(tmp_path / "out.mp4")
    job.media_type = "hls"
    return MediaDownloadEngine(job, "UA/1.0", MediaKind.HLS)


@pytest.fixture
def stream_output_ts(data_dir):
    """Write the real settings file (isolated by data_dir) and return a setter."""
    from magic_downloader import config

    def _set(value: bool) -> None:
        config.save_settings({**config.DEFAULT_SETTINGS, "stream_output_ts": bool(value)})

    return _set


@pytest.fixture
def ffmpeg_cache():
    """Keep ffmpeg's module-level lookup cache from leaking between tests."""
    from magic_downloader.media import ffmpeg

    ffmpeg.reset_cache()
    yield ffmpeg
    ffmpeg.reset_cache()


def _write_segments(track_dir: Path, count: int, size: int = 4096) -> bytes:
    """Create ``count`` fake segment files; return their concatenation."""
    track_dir.mkdir(parents=True, exist_ok=True)
    blob = b""
    for i in range(count):
        data = bytes([65 + i]) * size
        (track_dir / f"{i:06d}.seg").write_bytes(data)
        blob += data
    return blob


def _build_track(root: Path, kind: str, ext: str, nsegs: int = 3):
    """A _Track whose segments already sit on disk, ready for _concat_track."""
    from magic_downloader.media.media_engine import _Seg, _Track

    track_dir = root / kind
    blob = _write_segments(track_dir, nsegs, size=1024)
    track = _Track(kind=kind, ext=ext)
    track.segments = [_Seg(index=i, url=f"https://cdn.test/{kind}/{i}") for i in range(nsegs)]
    track.out_path = track_dir / f"track{ext}"
    return track, blob


# ───────────────────────────── detect ─────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://cdn.test/vod/master.m3u8", "hls"),
        ("https://cdn.test/vod/old.m3u", "hls"),
        ("https://cdn.test/dash/manifest.mpd", "dash"),
        ("https://cdn.test/files/movie.mp4", "http"),
        ("https://cdn.test/files/song.MP3", "http"),
        ("https://cdn.test/files/archive.zip", "http"),
        # query string after the extension must not confuse the classifier
        ("https://cdn.test/vod/master.m3u8?token=abc&expires=1", "hls"),
        ("https://cdn.test/dash/manifest.mpd?sig=xyz", "dash"),
        ("https://cdn.test/files/movie.mp4?range=1", "http"),
        # extension hidden inside the query string only
        ("https://cdn.test/manifest?path=/vod/x.m3u8&t=1", "hls"),
        ("https://cdn.test/manifest?path=/vod/x.mpd&t=1", "dash"),
        # no extension at all
        ("https://cdn.test/watch/12345", "http"),
    ],
)
def test_classify_url_from_path_and_query(url, expected):
    from magic_downloader.media.detect import classify_url

    assert classify_url(url).value == expected


@pytest.mark.parametrize(
    "content_type,expected",
    [
        ("application/vnd.apple.mpegurl", "hls"),
        ("application/x-mpegURL; charset=utf-8", "hls"),
        ("audio/mpegurl", "hls"),
        ("application/dash+xml", "dash"),
        ("video/mp4", "http"),
        ("", "http"),
    ],
)
def test_content_type_overrides_the_extension(content_type, expected):
    """A .mp4 URL served as a manifest is a manifest: Content-Type wins."""
    from magic_downloader.media.detect import classify_url

    assert classify_url("https://cdn.test/files/movie.mp4", content_type).value == expected


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"media_type": "hls", "url": "https://cdn.test/x"}, "hls"),
        ({"media_type": "m3u8", "url": "https://cdn.test/x"}, "hls"),
        ({"media_type": "dash", "url": "https://cdn.test/x"}, "dash"),
        ({"media_type": "mpd", "url": "https://cdn.test/x"}, "dash"),
        ({"media_type": "page", "url": "https://site.test/watch"}, "page"),
        ({"kind": "ytdlp", "url": "https://site.test/watch"}, "page"),
        # An unknown/absent hint falls through to URL + Content-Type sniffing.
        ({"url": "https://cdn.test/vod/master.m3u8"}, "hls"),
        ({"media_type": "video", "url": "https://cdn.test/f.mp4"}, "http"),
        ({"url": "https://cdn.test/f", "content_type": "application/dash+xml"}, "dash"),
        # "progressive" is only a hint — the URL is authoritative for manifests.
        ({"media_type": "progressive", "url": "https://cdn.test/vod/master.m3u8"}, "hls"),
    ],
)
def test_detect_kind_honours_extension_hints(payload, expected):
    from magic_downloader.media.detect import detect_kind

    assert detect_kind(payload).value == expected


@pytest.mark.parametrize(
    "url,is_media",
    [
        ("https://cdn.test/a/b.mkv", True),
        ("https://cdn.test/a/b.MP4", True),
        ("https://cdn.test/a/b.flac", True),
        ("https://cdn.test/a/b.m4s", True),
        ("https://cdn.test/a/b.m3u8", False),
        ("https://cdn.test/a/b.html", False),
        ("https://cdn.test/a/b", False),
        ("https://cdn.test/a.mp4/b", False),  # extension must be in the last path part
    ],
)
def test_is_media_file(url, is_media):
    from magic_downloader.media.detect import is_media_file

    assert is_media_file(url) is is_media


def test_mp4_url_containing_m3u8_is_misclassified_as_hls():
    """Documents current behaviour (a false positive, not a desired result).

    The raw-URL sniff runs even when the path already has a progressive media
    extension, so a file literally named ``clip.m3u8.mp4`` is treated as a
    playlist. Harmless for real CDN URLs, wrong for this one.
    """
    from magic_downloader.media.detect import classify_url

    assert classify_url("https://cdn.test/dl/clip.m3u8.mp4").value == "hls"


# ───────────────────────────── hls ────────────────────────────────────────


def test_is_master_distinguishes_playlist_types():
    from magic_downloader.media import hls

    assert hls.is_master(MASTER_M3U8) is True
    assert hls.is_master(MEDIA_M3U8) is False
    assert hls.is_master(FMP4_M3U8) is False


def test_parse_master_variants_and_relative_urls():
    from magic_downloader.media import hls

    master = hls.parse_master(MASTER_M3U8, MASTER_URL)

    assert [v.height for v in master.variants] == [360, 1080, 720]
    assert [v.bandwidth for v in master.variants] == [800000, 3000000, 1500000]
    assert [v.url for v in master.variants] == [
        "https://cdn.test/vod/low/index.m3u8",      # relative → resolved
        "https://edge.cdn.test/high/index.m3u8",    # absolute → untouched
        MID_URL,
    ]
    # A quoted CODECS list contains a comma; it must not split the attributes.
    assert master.variants[1].codecs == "avc1.640028,mp4a.40.2"
    assert master.variants[1].frame_rate == "29.970"
    assert [v.audio_group for v in master.variants] == ["aac"] * 3
    assert [v.label() for v in master.variants] == ["360p", "1080p", "720p"]


def test_best_variant_picks_the_highest_resolution():
    from magic_downloader.media import hls

    master = hls.parse_master(MASTER_M3U8, MASTER_URL)
    best = master.best_variant()

    assert best is not None
    assert best.height == 1080
    assert best.url == "https://edge.cdn.test/high/index.m3u8"
    assert hls.HlsMaster().best_variant() is None


def test_best_variant_breaks_resolution_ties_on_bandwidth():
    from magic_downloader.media import hls

    text = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\n"
        "a.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=4500000,RESOLUTION=1280x720\n"
        "b.m3u8\n"
    )
    master = hls.parse_master(text, MASTER_URL)

    assert master.best_variant().url == "https://cdn.test/vod/b.m3u8"


def test_audio_for_prefers_the_default_rendition():
    from magic_downloader.media import hls

    text = (
        "#EXTM3U\n"
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aac",NAME="Spanish",LANGUAGE="es",URI="es.m3u8"\n'
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aac",NAME="English",LANGUAGE="en",DEFAULT=YES,URI="en.m3u8"\n'
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="ec3",NAME="Atmos",URI="atmos.m3u8"\n'
        "#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1x2,AUDIO=\"aac\"\n"
        "v.m3u8\n"
    )
    master = hls.parse_master(text, MASTER_URL)

    assert master.audio_for("aac").language == "en"
    assert master.audio_for("aac").uri == "https://cdn.test/vod/en.m3u8"
    assert master.audio_for("ec3").name == "Atmos"      # no DEFAULT → first match
    assert master.audio_for("nope") is None


def test_parse_media_segments_durations_and_url_resolution():
    from magic_downloader.media import hls

    pl = hls.parse_media(MEDIA_M3U8, MID_URL)

    assert pl.target_duration == 10.0
    assert [s.url for s in pl.segments] == [
        "https://cdn.test/vod/mid/seg0.ts",        # relative to the playlist
        "https://edge.cdn.test/mid/seg1.ts",       # absolute
        "https://cdn.test/vod/shared/seg2.ts",     # ../ walks up one level
    ]
    assert [s.duration for s in pl.segments] == [9.009, 9.009, 3.482]
    assert pl.total_duration == pytest.approx(21.5)
    # EXT-X-MEDIA-SEQUENCE:5 numbers the first segment 5 (used as the AES IV).
    assert [s.seq for s in pl.segments] == [5, 6, 7]
    assert pl.is_fmp4 is False
    assert all(s.key is None for s in pl.segments)


def test_parse_media_detects_fmp4_init_segment():
    from magic_downloader.media import hls

    pl = hls.parse_media(FMP4_M3U8, "https://cdn.test/vod/hi/index.m3u8")

    assert pl.is_fmp4 is True
    assert pl.map_uri == "https://cdn.test/vod/hi/init.mp4"
    assert [s.url for s in pl.segments] == [
        "https://cdn.test/vod/hi/seg-1.m4s",
        "https://cdn.test/vod/hi/seg-2.m4s",
    ]
    assert pl.total_duration == pytest.approx(12.0)


def test_parse_media_key_applies_until_method_none():
    from magic_downloader.media import hls

    pl = hls.parse_media(AES_M3U8, "https://cdn.test/vod/enc/index.m3u8")

    first, second = pl.segments
    assert first.key is not None
    assert first.key.method == "AES-128"
    assert first.key.uri == "https://key.test/k1.key"
    assert first.key.iv == "0x00000000000000000000000000000009"
    # METHOD=NONE clears the key for every later segment.
    assert second.key is None


def test_parse_media_explicit_byterange():
    from magic_downloader.media import hls

    pl = hls.parse_media(BYTERANGE_M3U8, "https://cdn.test/vod/br/index.m3u8")

    assert len(pl.segments) == 2
    assert (pl.segments[0].byte_length, pl.segments[0].byte_offset) == (1000, 0)
    assert pl.segments[1].byte_length == 2000
    assert all(s.url == "https://cdn.test/vod/br/whole.ts" for s in pl.segments)


# Was xfail: an EXT-X-BYTERANGE with no @offset left byte_offset None, so
# _fetch_segment fell back to offset 0 and every offset-less segment re-fetched
# the head of the file (RFC 8216 4.3.2.2).
def test_byterange_without_offset_continues_previous_segment():
    from magic_downloader.media import hls

    pl = hls.parse_media(BYTERANGE_M3U8, "https://cdn.test/vod/br/index.m3u8")

    assert pl.segments[1].byte_offset == 1000


def test_hls_segment_url_containing_mp4_is_flagged_fmp4():
    """Documents current behaviour: the fMP4 sniff matches ``.mp4`` anywhere.

    Wowza-style paths (``/vod/movie.mp4/chunk-1.ts``) serve plain MPEG-TS
    segments but are reported as fragmented MP4, which picks the wrong
    container extension for the assembled track.
    """
    from magic_downloader.media import hls

    text = "#EXTM3U\n#EXTINF:4.0,\nchunk-1.ts\n"
    pl = hls.parse_media(text, "https://cdn.test/vod/movie.mp4/index.m3u8")
    assert pl.is_fmp4 is False          # sniffs the segment line, not the base

    text2 = "#EXTM3U\n#EXTINF:4.0,\nmovie.mp4/chunk-1.ts\n"
    pl2 = hls.parse_media(text2, "https://cdn.test/vod/index.m3u8")
    assert pl2.is_fmp4 is True          # false positive: these are .ts segments


# ───────────────────────────── dash ───────────────────────────────────────


def test_parse_mpd_number_template_video_representations():
    from magic_downloader.media import dash

    manifest = dash.parse(MPD_NUMBER, MPD_URL)

    assert manifest.duration == pytest.approx(60.0)
    assert [r.id for r in manifest.video] == ["v720", "v1080"]
    assert [(r.width, r.height) for r in manifest.video] == [(1280, 720), (1920, 1080)]
    assert [r.bandwidth for r in manifest.video] == [1200000, 3000000]
    assert [r.label() for r in manifest.video] == ["720p", "1080p"]
    assert manifest.best_video().id == "v1080"

    v720 = manifest.video[0]
    # SegmentTemplate lives on the AdaptationSet and is inherited by both reps;
    # 60 s / 20 s segments = 3, numbered from startNumber with %03d padding.
    assert v720.init_url == "https://cdn.test/dash/v720/init.mp4"
    assert v720.segment_urls == [
        "https://cdn.test/dash/v720/seg-001.m4s",
        "https://cdn.test/dash/v720/seg-002.m4s",
        "https://cdn.test/dash/v720/seg-003.m4s",
    ]
    assert manifest.video[1].segment_urls[0] == "https://cdn.test/dash/v1080/seg-001.m4s"


def test_parse_mpd_audio_segment_list():
    from magic_downloader.media import dash

    manifest = dash.parse(MPD_NUMBER, MPD_URL)
    audio = manifest.best_audio()

    assert audio is not None and audio.id == "a128"
    assert audio.is_audio is True and audio.is_video is False
    assert audio.audio_rate == 48000
    assert audio.init_url == "https://cdn.test/dash/audio/init.mp4"
    assert audio.segment_urls == [
        f"https://cdn.test/dash/audio/seg-00{i}.m4s" for i in (1, 2, 3)
    ]


def test_parse_mpd_segment_timeline_and_base_url():
    from magic_downloader.media import dash

    manifest = dash.parse(MPD_TIMELINE, MPD_URL)

    assert manifest.duration == pytest.approx(3723.5)   # PT1H2M3.5S
    rep = manifest.video[0]
    # <S t=0 d=180000 r=2> = 3 segments, then one more of d=90000 continuing on.
    assert rep.segment_urls == [
        "https://edge.test/v/chunk-0.m4s",
        "https://edge.test/v/chunk-180000.m4s",
        "https://edge.test/v/chunk-360000.m4s",
        "https://edge.test/v/chunk-540000.m4s",
    ]
    assert rep.init_url == "https://edge.test/v/init.mp4"   # <BaseURL> inherited


MPD_REP_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT30S">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v480" bandwidth="700000" width="854" height="480">
        <SegmentTemplate initialization="v480/init.mp4" media="v480/seg-$Number$.m4s"
                         startNumber="1" timescale="1000" duration="10000"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


# Was xfail(strict) while the bug was live: dash.parse used
# `_find(rep, "SegmentTemplate") or aset_tmpl`, and an ElementTree element with
# no children is falsy — so a self-closing Representation-level
# <SegmentTemplate/> was discarded, the representation fell through to the
# bare-BaseURL branch, and its only "segment" became the .mpd URL itself: the app
# downloaded the manifest as the video. Fixed with an explicit `is not None`.
def test_representation_level_segment_template_is_used():
    from magic_downloader.media import dash

    rep = dash.parse(MPD_REP_TEMPLATE, MPD_URL).video[0]

    assert rep.init_url == "https://cdn.test/dash/v480/init.mp4"
    assert rep.segment_urls == [
        "https://cdn.test/dash/v480/seg-1.m4s",
        "https://cdn.test/dash/v480/seg-2.m4s",
        "https://cdn.test/dash/v480/seg-3.m4s",
    ]


def test_representation_template_plans_real_segments_not_the_manifest(engine, url_session):
    """The bug above, at the level the user felt it.

    While a self-closing Representation-level <SegmentTemplate/> was being
    dropped, planning yielded a single "segment" pointing at the manifest URL —
    so the finished download was the .mpd file instead of the video. Planning
    must produce the template's real segment URLs.
    """
    from magic_downloader.media.detect import MediaKind

    engine.media_kind = MediaKind.DASH
    engine.job.url = MPD_URL
    engine._session = url_session({MPD_URL: MPD_REP_TEMPLATE})

    (video,) = engine._plan()

    urls = [s.url for s in video.segments]
    assert MPD_URL not in urls, "the manifest itself must never be a media segment"
    assert urls == [
        "https://cdn.test/dash/v480/seg-1.m4s",
        "https://cdn.test/dash/v480/seg-2.m4s",
        "https://cdn.test/dash/v480/seg-3.m4s",
    ]


def test_parse_mpd_rejects_garbage():
    from magic_downloader.media import dash

    manifest = dash.parse("<MPD><not xml", MPD_URL)

    assert manifest.video == [] and manifest.audio == []
    assert manifest.best_video() is None and manifest.best_audio() is None


# ───────────────────────── media_engine: planning ─────────────────────────


def test_plan_hls_picks_requested_height_and_alternate_audio(engine, url_session):
    engine._session = url_session(
        {MASTER_URL: MASTER_M3U8, MID_URL: MEDIA_M3U8, AUDIO_URL: AUDIO_M3U8}
    )
    engine.job.media_meta = {"height": 720}

    tracks = engine._plan()

    assert [t.kind for t in tracks] == ["video", "audio"]
    video, audio = tracks
    assert video.ext == ".ts" and video.is_fmp4 is False and video.init_url == ""
    assert [s.url for s in video.segments] == [
        "https://cdn.test/vod/mid/seg0.ts",
        "https://edge.cdn.test/mid/seg1.ts",
        "https://cdn.test/vod/shared/seg2.ts",
    ]
    assert [s.seq for s in video.segments] == [5, 6, 7]
    assert len(audio.segments) == 2
    # The chosen variant (not the best one) drives the reported quality...
    assert engine.job.media_meta["quality"] == "720p"
    assert [v["label"] for v in engine.job.media_meta["variants"]] == ["360p", "1080p", "720p"]
    # ...and the size estimate: 1.5 Mbit/s over 21.5 s.
    assert engine.job.total_size == pytest.approx(1500000 / 8 * 21.5, rel=1e-6)


def test_plan_hls_media_playlist_without_master(engine, url_session):
    engine.job.url = MID_URL
    engine._session = url_session({MID_URL: MEDIA_M3U8})

    tracks = engine._plan()

    assert len(tracks) == 1 and tracks[0].kind == "video"
    assert len(tracks[0].segments) == 3
    assert "quality" not in engine.job.media_meta


def test_plan_hls_fmp4_track_carries_init_segment(engine, url_session):
    engine.job.url = "https://cdn.test/vod/hi/index.m3u8"
    engine._session = url_session({engine.job.url: FMP4_M3U8})

    (video,) = engine._plan()

    assert video.is_fmp4 is True
    assert video.ext == ".mp4"
    assert video.init_url == "https://cdn.test/vod/hi/init.mp4"


def test_plan_dash_builds_video_and_audio_tracks(engine, url_session):
    from magic_downloader.media.detect import MediaKind

    engine.media_kind = MediaKind.DASH
    engine.job.url = MPD_URL
    engine.job.media_meta = {"height": 720, "duration": 60}
    engine._session = url_session({MPD_URL: MPD_NUMBER})

    tracks = engine._plan()

    assert [t.kind for t in tracks] == ["video", "audio"]
    video, audio = tracks
    assert video.ext == ".mp4" and audio.ext == ".m4a"
    assert video.is_fmp4 is True
    assert video.init_url == "https://cdn.test/dash/v720/init.mp4"
    assert [s.url for s in video.segments] == [
        f"https://cdn.test/dash/v720/seg-00{i}.m4s" for i in (1, 2, 3)
    ]
    assert len(audio.segments) == 3
    assert engine.job.media_meta["quality"] == "720p"
    # video + audio bandwidth over the manifest duration
    assert engine.job.total_size == int((1200000 + 128000) / 8 * 60)


def test_plan_rejects_progressive_kind(engine):
    from magic_downloader.media.detect import MediaKind
    from magic_downloader.media.media_engine import MediaProcessingError

    engine.media_kind = MediaKind.HTTP
    with pytest.raises(MediaProcessingError):
        engine._plan()


def test_iv_defaults_to_the_media_sequence_number(engine):
    from magic_downloader.media import hls
    from magic_downloader.media.media_engine import _Seg

    explicit = _Seg(index=0, url="u", key=hls.HlsKey("AES-128", "k", "0x9"), seq=3)
    assert engine._iv_for(explicit) == bytes(15) + b"\x09"

    default = _Seg(index=1, url="u", key=hls.HlsKey("AES-128", "k", ""), seq=258)
    assert default == default  # dataclass sanity
    assert engine._iv_for(default) == bytes(14) + b"\x01\x02"   # 258 big-endian


def test_pkcs7_unpad_only_strips_real_padding():
    from magic_downloader.media.media_engine import _pkcs7_unpad

    assert _pkcs7_unpad(b"abc" + bytes([3, 3, 3])) == b"abc"
    assert _pkcs7_unpad(b"\x47" * 16) == b"\x47" * 16     # TS sync bytes, not padding
    assert _pkcs7_unpad(b"ab" + bytes([2, 3])) == b"ab" + bytes([2, 3])
    assert _pkcs7_unpad(b"") == b""


# ───────────────────── media_engine: streaming concat ─────────────────────


def test_concat_track_frees_each_segment_as_it_goes(tmp_path, engine, monkeypatch):
    """Peak temp usage must stay ~1x: a segment is deleted the moment it is
    folded into the track file, never after the whole track is written.

    Regression guard for the bug where a 6.5 GB video needed 13 GB of temp
    space (all segments plus the assembled copy alive at the same time).
    """
    from magic_downloader.media.media_engine import _Seg, _Track

    track_dir = tmp_path / "video"
    blob = _write_segments(track_dir, 4, size=4096)
    track = _Track(kind="video", ext=".ts")
    track.segments = [_Seg(index=i, url="") for i in range(4)]
    track.out_path = track_dir / "track.ts"

    events: list[tuple[str, str]] = []
    segments_alive_at_read: list[int] = []
    real_open = builtins.open
    real_unlink = Path.unlink

    def spy_open(file, mode="r", *args, **kw):
        name = os.path.basename(str(file))
        if name.endswith(".seg") and "r" in mode:
            events.append(("read", name))
            segments_alive_at_read.append(len(list(track_dir.glob("*.seg"))))
        return real_open(file, mode, *args, **kw)

    def spy_unlink(self, *args, **kw):
        if self.name.endswith(".seg"):
            events.append(("unlink", self.name))
        return real_unlink(self, *args, **kw)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(Path, "unlink", spy_unlink)

    engine._concat_track(track)

    monkeypatch.undo()

    # Read/delete strictly interleave — deleting at the end would give
    # four reads followed by four unlinks.
    assert events == [
        ("read", "000000.seg"), ("unlink", "000000.seg"),
        ("read", "000001.seg"), ("unlink", "000001.seg"),
        ("read", "000002.seg"), ("unlink", "000002.seg"),
        ("read", "000003.seg"), ("unlink", "000003.seg"),
    ]
    # ...so the segments still on disk shrink as the track file grows.
    assert segments_alive_at_read == [4, 3, 2, 1]
    assert track.out_path.read_bytes() == blob
    assert list(track_dir.glob("*.seg")) == []


def test_concat_track_prepends_and_frees_the_fmp4_init_segment(tmp_path, engine):
    from magic_downloader.media.media_engine import _Seg, _Track

    track_dir = tmp_path / "video"
    blob = _write_segments(track_dir, 3, size=512)
    init_path = track_dir / "init.seg"
    init_path.write_bytes(b"FTYPINIT")

    track = _Track(kind="video", ext=".mp4", is_fmp4=True)
    track.segments = [_Seg(index=i, url="") for i in range(3)]
    track.out_path = track_dir / "track.mp4"
    track._init_path = init_path

    engine._concat_track(track)

    assert track.out_path.read_bytes() == b"FTYPINIT" + blob
    assert not init_path.exists()          # the init copy is reclaimed too
    assert list(track_dir.glob("*.seg")) == []


def test_concat_track_skips_missing_segments(tmp_path, engine):
    """A vanished segment is skipped rather than raising mid-assembly."""
    from magic_downloader.media.media_engine import _Seg, _Track

    track_dir = tmp_path / "video"
    _write_segments(track_dir, 3, size=64)
    (track_dir / "000001.seg").unlink()

    track = _Track(kind="video", ext=".ts")
    track.segments = [_Seg(index=i, url="") for i in range(3)]
    track.out_path = track_dir / "track.ts"

    engine._concat_track(track)

    assert track.out_path.read_bytes() == bytes([65]) * 64 + bytes([67]) * 64


# ──────────────────── media_engine: .ts vs .mp4 assembly ──────────────────


@pytest.fixture
def ffmpeg_stub(monkeypatch):
    """Replace every ffmpeg entry point the engine uses with a recorder."""
    from magic_downloader.media import media_engine

    calls: list[tuple] = []

    def make(kind, fail=False, leave_partial=False):
        def _fn(inputs, output, copy=True):
            calls.append((kind, [str(i) for i in inputs], str(output), copy))
            if leave_partial:
                Path(output).write_bytes(b"broken header")
            if fail:
                raise RuntimeError(f"{kind} mux failed: codec not supported")
            Path(output).write_bytes(b"MUXED-" + kind.encode())
        return _fn

    def install(*, has_ffmpeg=True, ts_fails=False):
        monkeypatch.setattr(media_engine.ffmpeg, "has_ffmpeg", lambda: has_ffmpeg)
        monkeypatch.setattr(
            media_engine.ffmpeg, "mux_to_ts",
            make("ts", fail=ts_fails, leave_partial=ts_fails),
        )
        monkeypatch.setattr(media_engine.ffmpeg, "mux_to_mp4", make("mp4"))
        return calls

    return install


def test_assemble_muxes_to_mp4_by_default(tmp_path, engine, ffmpeg_stub, stream_output_ts):
    stream_output_ts(False)
    calls = ffmpeg_stub()
    video, vblob = _build_track(tmp_path / "tmp", "video", ".ts")
    audio, ablob = _build_track(tmp_path / "tmp", "audio", ".m4a")

    engine._assemble([video, audio])

    assert [c[0] for c in calls] == ["mp4"]
    assert calls[0][1] == [str(video.out_path), str(audio.out_path)]
    assert calls[0][2] == str(tmp_path / "out.mp4")
    assert engine.job.save_path == str(tmp_path / "out.mp4")
    # Both tracks were concatenated first, and their segments reclaimed.
    assert video.out_path.read_bytes() == vblob
    assert audio.out_path.read_bytes() == ablob
    assert list((tmp_path / "tmp" / "video").glob("*.seg")) == []


def test_assemble_writes_ts_when_stream_output_ts_is_on(
    tmp_path, engine, ffmpeg_stub, stream_output_ts
):
    stream_output_ts(True)
    calls = ffmpeg_stub()
    video, _ = _build_track(tmp_path / "tmp", "video", ".ts")
    audio, _ = _build_track(tmp_path / "tmp", "audio", ".m4a")

    engine._assemble([video, audio])

    assert [c[0] for c in calls] == ["ts"]
    assert calls[0][2] == str(tmp_path / "out.ts")
    assert engine.job.save_path == str(tmp_path / "out.ts")
    assert engine.job.filename == "out.ts"
    assert (tmp_path / "out.ts").read_bytes() == b"MUXED-ts"


def test_assemble_ts_setting_is_ignored_for_audio_only_jobs(
    tmp_path, engine, ffmpeg_stub, stream_output_ts
):
    """Raw .ts output only makes sense for video; audio still lands as .mp4."""
    stream_output_ts(True)
    calls = ffmpeg_stub()
    audio, _ = _build_track(tmp_path / "tmp", "audio", ".m4a")

    engine._assemble([audio])

    assert [c[0] for c in calls] == ["mp4"]
    assert engine.job.save_path == str(tmp_path / "out.mp4")


def test_assemble_falls_back_to_mp4_when_the_ts_mux_fails(
    tmp_path, engine, ffmpeg_stub, stream_output_ts
):
    """MPEG-TS cannot carry VP9/AV1/Opus — a failed .ts mux must not throw away
    a fully downloaded stream; it re-muxes into .mp4 and retargets the job."""
    stream_output_ts(True)
    calls = ffmpeg_stub(ts_fails=True)
    video, _ = _build_track(tmp_path / "tmp", "video", ".ts")
    audio, _ = _build_track(tmp_path / "tmp", "audio", ".m4a")

    engine._assemble([video, audio])

    assert [c[0] for c in calls] == ["ts", "mp4"]
    assert calls[1][2] == str(tmp_path / "out.mp4")
    assert engine.job.save_path == str(tmp_path / "out.mp4")
    assert engine.job.filename == "out.mp4"
    assert (tmp_path / "out.mp4").read_bytes() == b"MUXED-mp4"
    # the half-written .ts ffmpeg left behind is cleaned up
    assert not (tmp_path / "out.ts").exists()


def test_assemble_reports_a_failing_mp4_mux(tmp_path, engine, ffmpeg_stub, stream_output_ts,
                                            monkeypatch):
    from magic_downloader.media import media_engine

    stream_output_ts(False)
    ffmpeg_stub()

    def boom(inputs, output, copy=True):
        raise RuntimeError("Invalid data found when processing input")

    monkeypatch.setattr(media_engine.ffmpeg, "mux_to_mp4", boom)
    video, _ = _build_track(tmp_path / "tmp", "video", ".ts")

    with pytest.raises(media_engine.MediaProcessingError, match="ffmpeg failed"):
        engine._assemble([video])


def test_assemble_without_ffmpeg_delivers_a_playable_ts(
    tmp_path, engine, ffmpeg_stub, stream_output_ts
):
    stream_output_ts(False)
    ffmpeg_stub(has_ffmpeg=False)
    video, vblob = _build_track(tmp_path / "tmp", "video", ".ts")

    engine._assemble([video])

    final = tmp_path / "out.ts"
    assert engine.job.save_path == str(final)
    assert engine.job.filename == "out.ts"
    assert final.read_bytes() == vblob
    assert "ffmpeg" in engine.job.media_meta["ffmpeg_note"]


def test_assemble_without_ffmpeg_keeps_audio_next_to_the_video(
    tmp_path, engine, ffmpeg_stub, stream_output_ts
):
    """Nothing is lost when the two tracks cannot be muxed.

    Note the primary file keeps its .mp4 name even though the payload is raw
    MPEG-TS here (unlike the video-only path, which renames to .ts).
    """
    stream_output_ts(False)
    ffmpeg_stub(has_ffmpeg=False)
    video, vblob = _build_track(tmp_path / "tmp", "video", ".ts")
    audio, ablob = _build_track(tmp_path / "tmp", "audio", ".m4a")

    engine._assemble([video, audio])

    assert (tmp_path / "out.mp4").read_bytes() == vblob
    assert (tmp_path / "out.audio.m4a").read_bytes() == ablob
    assert "install ffmpeg" in engine.job.media_meta["ffmpeg_note"]
    assert engine.job.error == ""


def test_want_ts_output_survives_a_corrupt_settings_file(engine, data_dir, monkeypatch):
    from magic_downloader import config

    monkeypatch.setattr(config, "load_settings", lambda: (_ for _ in ()).throw(OSError("boom")))
    assert engine._want_ts_output() is False


# ───────────────────────────── ffmpeg ─────────────────────────────────────


def test_mux_to_mp4_retries_without_the_aac_bitstream_filter(ffmpeg_cache, monkeypatch):
    ffmpeg = ffmpeg_cache
    monkeypatch.setattr(ffmpeg, "find_ffmpeg", lambda: "ffmpeg.exe")
    runs: list[list[str]] = []

    def fake_run(args, timeout=None):
        runs.append(args)
        return SimpleNamespace(returncode=0 if len(runs) > 1 else 1, stdout=b"", stderr=b"bad bsf")

    monkeypatch.setattr(ffmpeg, "_run", fake_run)

    ffmpeg.mux_to_mp4(["v.ts", "a.m4a"], "out.mp4")

    assert len(runs) == 2
    assert runs[0][:2] == ["ffmpeg.exe", "-y"]
    assert runs[0].count("-i") == 2 and runs[0][-1] == "out.mp4"
    assert "-bsf:a" in runs[0] and "aac_adtstoasc" in runs[0]
    assert "-bsf:a" not in runs[1]            # the retry drops the filter
    assert "+faststart" in runs[1]


def test_mux_to_mp4_raises_with_ffmpeg_stderr_when_both_attempts_fail(ffmpeg_cache, monkeypatch):
    ffmpeg = ffmpeg_cache
    monkeypatch.setattr(ffmpeg, "find_ffmpeg", lambda: "ffmpeg.exe")
    monkeypatch.setattr(
        ffmpeg, "_run",
        lambda args, timeout=None: SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"Invalid data found"
        ),
    )

    with pytest.raises(RuntimeError, match="Invalid data found"):
        ffmpeg.mux_to_mp4(["v.ts"], "out.mp4")


def test_mux_to_ts_requests_the_mpegts_container(ffmpeg_cache, monkeypatch):
    ffmpeg = ffmpeg_cache
    monkeypatch.setattr(ffmpeg, "find_ffmpeg", lambda: "ffmpeg.exe")
    runs: list[list[str]] = []

    def fake_run(args, timeout=None):
        runs.append(args)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(ffmpeg, "_run", fake_run)

    ffmpeg.mux_to_ts(["v.mp4", "a.m4a"], "out.ts")

    assert len(runs) == 1                      # no retry path for TS
    assert runs[0][-3:] == ["-f", "mpegts", "out.ts"]
    assert "-c" in runs[0] and "copy" in runs[0]


def test_mux_to_ts_raises_so_the_caller_can_fall_back(ffmpeg_cache, monkeypatch):
    """The .ts → .mp4 fallback in media_engine hangs off this RuntimeError."""
    ffmpeg = ffmpeg_cache
    monkeypatch.setattr(ffmpeg, "find_ffmpeg", lambda: "ffmpeg.exe")
    monkeypatch.setattr(
        ffmpeg, "_run",
        lambda args, timeout=None: SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"Could not find tag for codec vp9"
        ),
    )

    with pytest.raises(RuntimeError, match="vp9"):
        ffmpeg.mux_to_ts(["v.mp4"], "out.ts")


def test_mux_without_ffmpeg_raises(ffmpeg_cache, monkeypatch):
    ffmpeg = ffmpeg_cache
    monkeypatch.setattr(ffmpeg, "find_ffmpeg", lambda: None)
    monkeypatch.setattr(
        ffmpeg, "_run",
        lambda *a, **kw: pytest.fail("ffmpeg must not be invoked when it is missing"),
    )

    with pytest.raises(RuntimeError, match="not found"):
        ffmpeg.mux_to_mp4(["v.ts"], "out.mp4")
    with pytest.raises(RuntimeError, match="not found"):
        ffmpeg.mux_to_ts(["v.ts"], "out.ts")


def test_find_ffmpeg_prefers_the_bundled_copy_over_path(tmp_path, ffmpeg_cache, monkeypatch):
    ffmpeg = ffmpeg_cache
    import magic_downloader.paths as paths

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    bundled = bin_dir / exe
    bundled.write_bytes(b"stub")
    monkeypatch.setattr(paths, "BIN_DIR", bin_dir, raising=False)
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: "C:/elsewhere/ffmpeg.exe")

    assert ffmpeg.find_ffmpeg() == str(bundled)
    assert ffmpeg.has_ffmpeg() is True

    # The result is cached: deleting it does not change the answer until reset.
    bundled.unlink()
    assert ffmpeg.find_ffmpeg() == str(bundled)
    ffmpeg.reset_cache()
    assert ffmpeg.find_ffmpeg() == "C:/elsewhere/ffmpeg.exe"


def test_find_ffmpeg_accepts_an_explicit_hint(tmp_path, ffmpeg_cache, monkeypatch):
    ffmpeg = ffmpeg_cache
    monkeypatch.setattr(ffmpeg, "_app_bundled_dir", lambda: tmp_path / "empty")
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: None)
    custom = tmp_path / "tools" / "ffmpeg.exe"
    custom.parent.mkdir()
    custom.write_bytes(b"stub")

    assert ffmpeg.find_ffmpeg(str(custom)) == str(custom)
    # A hint that does not exist must not poison the cached result.
    assert ffmpeg.find_ffmpeg(str(tmp_path / "nope.exe")) == str(custom)


@pytest.mark.skipif(sys.platform != "win32", reason="checks the Windows hint list")
def test_find_ffmpeg_returns_none_when_nothing_is_installed(tmp_path, ffmpeg_cache, monkeypatch):
    ffmpeg = ffmpeg_cache
    monkeypatch.setattr(ffmpeg, "_app_bundled_dir", lambda: tmp_path / "empty")
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: None)
    monkeypatch.setattr(ffmpeg, "_WINDOWS_HINTS", [str(tmp_path / "missing" / "ffmpeg.exe")])

    assert ffmpeg.find_ffmpeg() is None
    assert ffmpeg.has_ffmpeg() is False


# ───────────────────────────── probe ──────────────────────────────────────


def _install_fake_requests(monkeypatch, session):
    import requests

    from magic_downloader.media import probe

    monkeypatch.setattr(
        probe,
        "requests",
        SimpleNamespace(Session=lambda: session, RequestException=requests.RequestException),
    )
    return probe


def test_probe_master_lists_variants_best_first_with_sizes(monkeypatch, url_session):
    session = url_session({MASTER_URL: MASTER_M3U8, "https://edge.cdn.test/high/index.m3u8": MEDIA_M3U8})
    probe = _install_fake_requests(monkeypatch, session)

    result = probe.probe_media(MASTER_URL, media_type="hls")

    assert result["kind"] == "hls"
    assert result["duration"] == 21          # int(21.5) from the best variant
    assert [v["height"] for v in result["variants"]] == [1080, 720, 360]
    assert [v["width"] for v in result["variants"]] == [1920, 1280, 640]
    assert result["variants"][0]["fps"] == "29.970"
    assert result["variants"][0]["filesize"] == int(3000000 / 8 * 21)
    assert all(v["approx"] for v in result["variants"])
    # Exactly two GETs: the master and the best variant (no segment downloads).
    assert [c[1] for c in session.calls] == [MASTER_URL, "https://edge.cdn.test/high/index.m3u8"]


def test_probe_media_playlist_without_master(monkeypatch, url_session):
    session = url_session({MID_URL: MEDIA_M3U8})
    probe = _install_fake_requests(monkeypatch, session)

    result = probe.probe_media(MID_URL)

    assert result["kind"] == "hls"
    assert result["duration"] == 21
    assert [v["label"] for v in result["variants"]] == ["source"]


def test_probe_dash_manifest(monkeypatch, url_session):
    session = url_session({MPD_URL: MPD_NUMBER})
    probe = _install_fake_requests(monkeypatch, session)

    result = probe.probe_media(MPD_URL)

    assert result["kind"] == "dash"
    assert result["duration"] == 60
    assert [v["label"] for v in result["variants"]] == ["1080p", "720p"]
    assert result["variants"][0]["filesize"] == int(3000000 / 8 * 60)


def test_probe_progressive_url_makes_no_request(monkeypatch, url_session):
    session = url_session({})
    probe = _install_fake_requests(monkeypatch, session)

    result = probe.probe_media("https://cdn.test/files/movie.mp4")

    assert result == {
        "kind": "http",
        "url": "https://cdn.test/files/movie.mp4",
        "variants": [],
        "duration": 0,
    }
    assert session.calls == []
