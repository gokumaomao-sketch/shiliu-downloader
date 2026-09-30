"""A live broadcast must fail honestly, not deliver a 30-second clip.

A live HLS/DASH manifest only advertises its current sliding window, so the
engine downloaded those few segments, reported COMPLETE, and handed the user a
fragment of an hours-long stream with no warning.

The risk in fixing this is the opposite error — refusing a VOD that downloads
fine today — so most of these tests guard the negative cases.
"""

from __future__ import annotations

import pytest

from magic_downloader.media import dash, hls


# --------------------------------------------------------------------- HLS

LIVE = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:26400
#EXTINF:10.0,
seg26400.ts
#EXTINF:10.0,
seg26401.ts
#EXTINF:10.0,
seg26402.ts
"""

VOD = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
a.ts
#EXTINF:10.0,
b.ts
#EXT-X-ENDLIST
"""

VOD_NO_ENDLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
a.ts
#EXTINF:10.0,
b.ts
"""

EVENT = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-PLAYLIST-TYPE:EVENT
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
a.ts
#EXTINF:10.0,
b.ts
"""


def test_a_live_playlist_is_flagged():
    pl = hls.parse_media(LIVE, "https://cdn.test/live.m3u8")
    assert pl.is_live is True


def test_a_finished_playlist_is_not_live():
    pl = hls.parse_media(VOD, "https://cdn.test/vod.m3u8")
    assert pl.is_live is False


def test_vod_without_endlist_is_not_live():
    """Some servers omit ENDLIST but declare PLAYLIST-TYPE:VOD — downloadable."""
    pl = hls.parse_media(VOD_NO_ENDLIST, "https://cdn.test/vod.m3u8")
    assert pl.is_live is False, "a declared VOD must not be refused"


def test_event_playlists_are_downloadable():
    """EVENT playlists only ever append, so what is published is all there."""
    pl = hls.parse_media(EVENT, "https://cdn.test/event.m3u8")
    assert pl.is_live is False


def test_liveness_does_not_break_normal_parsing():
    pl = hls.parse_media(VOD, "https://cdn.test/vod.m3u8")
    assert len(pl.segments) == 2
    assert pl.total_duration == pytest.approx(20.0)


# -------------------------------------------------------------------- DASH

def _mpd(type_attr="static", duration_attr='mediaPresentationDuration="PT30M"'):
    return f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="{type_attr}" {duration_attr}>
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v0" bandwidth="800000" width="640" height="360">
        <BaseURL>video.mp4</BaseURL>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


def test_dynamic_mpd_without_duration_is_live():
    m = dash.parse(_mpd("dynamic", ""), "https://cdn.test/live.mpd")
    assert m.is_live is True


def test_static_mpd_is_not_live():
    m = dash.parse(_mpd("static"), "https://cdn.test/vod.mpd")
    assert m.is_live is False


def test_a_finished_live_event_is_downloadable():
    """type stays dynamic once an event ends, but it gains a total duration."""
    m = dash.parse(_mpd("dynamic"), "https://cdn.test/ended.mpd")
    assert m.is_live is False, "a finished broadcast must still download"


def test_mpd_without_a_type_defaults_to_static():
    m = dash.parse(_mpd_no_type(), "https://cdn.test/vod.mpd")
    assert m.is_live is False


def _mpd_no_type():
    return """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT10M">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v0" bandwidth="800000" width="640" height="360">
        <BaseURL>video.mp4</BaseURL>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


# ------------------------------------------------- the engine refuses them

def _engine(url):
    from magic_downloader.media.media_engine import MediaDownloadEngine
    from magic_downloader.models import DownloadJob

    eng = MediaDownloadEngine.__new__(MediaDownloadEngine)
    eng.job = DownloadJob(url=url, save_path="C:/dl/x.mp4", filename="x.mp4")
    eng.job.media_meta = {}
    return eng


def test_planning_a_live_hls_refuses_with_a_clear_message(monkeypatch):
    from magic_downloader.media.media_engine import MediaProcessingError

    eng = _engine("https://cdn.test/live.m3u8")
    monkeypatch.setattr(type(eng), "_get_text", lambda self, url: LIVE, raising=False)

    with pytest.raises(MediaProcessingError) as err:
        eng._plan_hls()
    assert "live stream" in str(err.value).lower()


def test_planning_a_live_dash_refuses(monkeypatch):
    from magic_downloader.media.media_engine import MediaProcessingError

    eng = _engine("https://cdn.test/live.mpd")
    monkeypatch.setattr(type(eng), "_get_text", lambda self, url: _mpd("dynamic", ""),
                        raising=False)

    with pytest.raises(MediaProcessingError) as err:
        eng._plan_dash()
    assert "live stream" in str(err.value).lower()


def test_planning_a_vod_hls_still_works(monkeypatch):
    eng = _engine("https://cdn.test/vod.m3u8")
    monkeypatch.setattr(type(eng), "_get_text", lambda self, url: VOD, raising=False)
    monkeypatch.setattr(type(eng), "_estimate_size", lambda self, *a: None, raising=False)

    tracks = eng._plan_hls()
    assert tracks and tracks[0].segments, "a normal VOD must still plan normally"
