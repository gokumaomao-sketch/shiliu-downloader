"""Filename resolution and the "the server sent a web page" guard.

Two real bugs live here and must never come back:

1. A ``text/html`` response used to be turned into a ``.html`` filename. That
   produced junk files like ``identifier.html`` — and worse, once the job had
   been *renamed* to ``.html`` the HTML guard concluded a web page was what the
   user wanted and waved the download through as a success.
2. ``probe()`` must refuse an HTML body when the user asked for a file (a
   sign-in wall / consent interstitial) and say so in terms the user can act on,
   while still allowing the three genuine cases: an ``.html`` URL, an explicit
   ``Content-Disposition: attachment``, and a server that names an HTML file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from magic_downloader.engine import (
    DownloadEngine,
    _dedupe,
    _ext_from_content_type,
    _filename_from_content_disposition,
    _filename_from_url,
    _is_html_response,
    _resolve_name,
    _sanitize_name,
    _wants_html,
    looks_like_junk_name,
    suggest_filename,
)
from magic_downloader.models import DownloadStatus

HTML_BLOCK_HINT = "播放器旁"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
@pytest.fixture
def probe_engine(fake_session):
    """A DownloadEngine wired to a canned response, with no real socket.

    probe() only ever touches ``job``, ``_session``, ``_emit``/``on_progress``
    and ``_headers``/``user_agent``, so the constructor (which opens a real
    requests.Session) is bypassed deliberately.
    """

    def _build(job, response, user_agent="MagicTest/1.0"):
        eng = DownloadEngine.__new__(DownloadEngine)
        eng.job = job
        eng.user_agent = user_agent
        eng.on_progress = None
        eng._session = fake_session(response)
        return eng

    return _build


@pytest.fixture
def html_response(fake_response):
    """text/html, no Content-Disposition, at a URL with no .html in the path."""

    def _build(url="https://site.test/download?id=9", **headers):
        h = {"Content-Type": "text/html; charset=utf-8"}
        h.update(headers)
        return fake_response(url=url, headers=h)

    return _build


# --------------------------------------------------------------------------
# _filename_from_url / suggest_filename
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://ex.test/a/b/setup.exe", "setup.exe"),
        ("https://ex.test/a/b/My%20Report%202024.pdf", "My Report 2024.pdf"),
        ("https://ex.test/files/movie.mp4?token=abc&x=1", "movie.mp4"),
        ("https://ex.test/some/folder/", "folder"),
        ("https://ex.test/", "download"),
        ("https://ex.test", "download"),
    ],
)
def test_filename_from_url(url, expected):
    assert _filename_from_url(url) == expected


def test_suggest_filename_is_the_url_name():
    assert suggest_filename("https://ex.test/dir/archive%20(1).zip") == "archive (1).zip"


# --------------------------------------------------------------------------
# looks_like_junk_name
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "download",
        "Download",
        "VIDEO",
        "file",
        "somefilewithnoextension",
        "a5de8a50-7e22-4163-97a4-914d6644caa7",          # bare GUID
        "A5DE8A50-7E22-4163-97A4-914D6644CAA7.bin",      # GUID + extension
        "0123456789abcdef0123456789.mp4",                # long hex blob
        "clip.$$$",                                      # non-alphanumeric ext
    ],
)
def test_junk_names_are_detected(name):
    assert looks_like_junk_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "movie.mp4",
        "Annual Report.pdf",
        "archive.tar.gz",
        "song.m4a",
        "a5de8a50.mp4",          # hex, but far too short to be a hash
        "downloads.zip",         # only the bare word "download" is junk
        "video-2.mkv",
    ],
)
def test_real_names_are_not_junk(name):
    assert looks_like_junk_name(name) is False


# Was xfail: extensions longer than five characters were classed as junk, so a
# good filename could be replaced by whatever the server sent.
def test_long_but_legitimate_extensions_should_not_be_junk():
    assert looks_like_junk_name("ubuntu-24.04.1.torrent") is False
    assert looks_like_junk_name("Krita-5.2.AppImage") is False


# --------------------------------------------------------------------------
# _ext_from_content_type — must NEVER hand back ".html"
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ct",
    [
        "text/html",
        "text/html; charset=utf-8",
        "TEXT/HTML",
        "  text/html ; charset=ISO-8859-1",
        "application/xhtml+xml",
    ],
)
def test_html_content_type_never_produces_an_extension(ct):
    """Auto-naming a download ".html" is what created junk files."""
    assert _ext_from_content_type(ct) == ""


@pytest.mark.parametrize(
    "ct",
    ["", "application/octet-stream", "binary/octet-stream", "application/download"],
)
def test_generic_content_types_give_no_extension(ct):
    assert _ext_from_content_type(ct) == ""


@pytest.mark.parametrize(
    "ct, ext",
    [
        ("video/mp4", ".mp4"),
        ("video/mp4; charset=utf-8", ".mp4"),
        ("application/pdf", ".pdf"),
        ("image/jpeg", ".jpg"),          # never the ".jpe" mimetypes offers
        ("application/zip", ".zip"),
        ("audio/mpeg", ".mp3"),
    ],
)
def test_known_content_types_map_to_an_extension(ct, ext):
    assert _ext_from_content_type(ct) == ext


# --------------------------------------------------------------------------
# _filename_from_content_disposition
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cd, expected",
    [
        ('attachment; filename="report.pdf"', "report.pdf"),
        ("attachment; filename=report.pdf", "report.pdf"),
        ("attachment; filename=report.pdf; size=42", "report.pdf"),
        ("ATTACHMENT; FileName=\"Report Final.pdf\"", "Report Final.pdf"),
        ("attachment; filename*=UTF-8''my%20file.zip", "my file.zip"),
        ("inline; filename=\"page.html\"", "page.html"),
        ("attachment", None),
        ("", None),
        ("inline", None),
    ],
)
def test_filename_from_content_disposition(cd, expected):
    assert _filename_from_content_disposition(cd) == expected


def test_rfc5987_name_wins_over_the_ascii_fallback():
    cd = "attachment; filename=\"fallback.bin\"; filename*=UTF-8''real%20name.zip"
    assert _filename_from_content_disposition(cd) == "real name.zip"


# --------------------------------------------------------------------------
# _is_html_response
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ct",
    ["text/html", "text/html; charset=utf-8", "TEXT/HTML", " text/html", "application/xhtml+xml"],
)
def test_is_html_response_true(ct):
    assert _is_html_response(ct) is True


@pytest.mark.parametrize(
    "ct",
    ["", "video/mp4", "application/octet-stream", "text/plain", "application/pdf"],
)
def test_is_html_response_false(ct):
    assert _is_html_response(ct) is False


# --------------------------------------------------------------------------
# _wants_html — judged from the server + the clicked URL only
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cd, url",
    [
        ("attachment", "https://site.test/download"),
        ('attachment; filename="setup.exe"', "https://site.test/dl?id=1"),
        ('ATTACHMENT; filename="x.bin"', "https://site.test/dl"),
        ("", "https://site.test/article.html"),
        ("", "https://site.test/article.htm"),
        ("", "https://site.test/page.xhtml"),
        ("", "https://site.test/saved.mhtml"),
        ("", "https://site.test/article.HTML?ref=1#top"),
        ('inline; filename="index.html"', "https://site.test/view"),
    ],
)
def test_wants_html_true(cd, url):
    assert _wants_html(cd, url) is True


@pytest.mark.parametrize(
    "cd, url",
    [
        ("", "https://site.test/download?id=9"),
        ("", "https://site.test/files/movie.mp4"),
        ("inline", "https://site.test/view"),
        ('inline; filename="movie.mp4"', "https://site.test/view"),
        ("", "https://site.test/htmlfiles/report"),   # "html" in the path, not the ext
    ],
)
def test_wants_html_false(cd, url):
    assert _wants_html(cd, url) is False


def test_wants_html_ignores_the_local_filename():
    """The regression that let "identifier.html" through.

    The signature takes only (disposition, url) precisely so a name that was
    itself derived from the HTML response can never vote for its own approval.
    """
    assert _wants_html("", "https://site.test/download?id=9") is False


# --------------------------------------------------------------------------
# _sanitize_name
# --------------------------------------------------------------------------
def test_sanitize_replaces_every_illegal_windows_character():
    out = _sanitize_name('a<b>c:d"e/f\\g|h?i*j.zip')
    assert out == "a_b_c_d_e_f_g_h_i_j.zip"


def test_sanitize_defeats_directory_traversal():
    out = _sanitize_name("../../../Windows/System32/evil.exe")
    assert "/" not in out and "\\" not in out


def test_sanitize_trims_space_and_dots():
    assert _sanitize_name("   spaced out.txt  ") == "spaced out.txt"
    assert _sanitize_name("trailing.txt...") == "trailing.txt"


def test_sanitize_never_returns_an_empty_name():
    assert _sanitize_name("...") == "download"
    assert _sanitize_name("   ") == "download"


def test_sanitize_caps_the_length():
    assert len(_sanitize_name("x" * 400 + ".zip")) == 200


# Was xfail: the 200-char cap sliced the extension off a very long name, so the
# file was saved with none at all. The stem is truncated now, not the name.
def test_sanitize_should_keep_the_extension_when_truncating():
    assert _sanitize_name("x" * 400 + ".zip").endswith(".zip")


# --------------------------------------------------------------------------
# _dedupe
# --------------------------------------------------------------------------
def test_dedupe_leaves_a_free_name_alone(tmp_path):
    target = tmp_path / "movie.mp4"
    assert _dedupe(target) == target


def test_dedupe_avoids_an_existing_file(tmp_path):
    (tmp_path / "movie.mp4").write_bytes(b"old")
    assert _dedupe(tmp_path / "movie.mp4") == tmp_path / "movie (1).mp4"


def test_dedupe_avoids_an_in_progress_part_file(tmp_path):
    """A .part file means another download already owns that name."""
    (tmp_path / "movie.mp4.part").write_bytes(b"")
    assert _dedupe(tmp_path / "movie.mp4") == tmp_path / "movie (1).mp4"


def test_dedupe_keeps_counting_past_taken_numbers(tmp_path):
    (tmp_path / "movie.mp4").write_bytes(b"")
    (tmp_path / "movie (1).mp4").write_bytes(b"")
    (tmp_path / "movie (2).mp4.part").write_bytes(b"")
    assert _dedupe(tmp_path / "movie.mp4") == tmp_path / "movie (3).mp4"


def test_dedupe_handles_an_extensionless_name(tmp_path):
    (tmp_path / "payload").write_bytes(b"")
    assert _dedupe(tmp_path / "payload") == tmp_path / "payload (1)"


# --------------------------------------------------------------------------
# _resolve_name
# --------------------------------------------------------------------------
def test_resolve_keeps_a_good_local_name(tmp_path):
    assert _resolve_name("movie.mp4", 'attachment; filename="ad.bin"', "video/mp4", "https://s/x") is None


def test_resolve_uses_the_server_name_for_a_junk_one():
    got = _resolve_name(
        "a5de8a50-7e22-4163-97a4-914d6644caa7",
        'attachment; filename="Holiday.mp4"',
        "video/mp4",
        "https://s/a5de8a50-7e22-4163-97a4-914d6644caa7",
    )
    assert got == "Holiday.mp4"


def test_resolve_falls_back_to_the_final_url():
    got = _resolve_name("download", "", "application/octet-stream", "https://cdn.test/f/real-name.iso")
    assert got == "real-name.iso"


def test_resolve_adds_an_extension_from_the_content_type():
    got = _resolve_name("clip", "", "video/mp4", "https://s/watch?v=abc")
    assert got == "clip.mp4"


def test_resolve_never_invents_a_html_name():
    """The exact source of the junk "identifier.html" files."""
    got = _resolve_name("identifier", "", "text/html; charset=utf-8", "https://site.test/download?id=9")
    assert got is None


def test_resolve_gives_up_when_nothing_is_known():
    assert _resolve_name("blob", "", "application/octet-stream", "https://s/download") is None


# --------------------------------------------------------------------------
# probe(): blocking a web page served instead of the file
# --------------------------------------------------------------------------
def test_probe_blocks_a_sign_in_page(probe_engine, html_response, make_job, tmp_path):
    job = make_job(filename="download", save_path=str(tmp_path / "download"))
    eng = probe_engine(job, html_response())

    with pytest.raises(RuntimeError) as err:
        eng.probe()

    assert HTML_BLOCK_HINT in str(err.value).lower(), "the message must point at the cookie setting"


def test_probe_block_leaves_the_name_untouched(probe_engine, html_response, make_job, tmp_path):
    job = make_job(filename="download", save_path=str(tmp_path / "download"))
    eng = probe_engine(job, html_response())

    with pytest.raises(RuntimeError):
        eng.probe()

    assert job.filename == "download"
    assert not job.filename.endswith(".html")
    assert not job.save_path.endswith(".html")


def test_probe_blocks_even_when_the_job_is_already_named_html(
    probe_engine, html_response, make_job, tmp_path
):
    """The regression: a name derived from the page must not authorise the page."""
    job = make_job(filename="identifier.html", save_path=str(tmp_path / "identifier.html"))
    eng = probe_engine(job, html_response())

    with pytest.raises(RuntimeError):
        eng.probe()


def test_probe_blocks_xhtml_too(probe_engine, fake_response, make_job, tmp_path):
    job = make_job(filename="download", save_path=str(tmp_path / "download"))
    resp = fake_response(
        url="https://site.test/get?id=4", headers={"Content-Type": "application/xhtml+xml"}
    )
    eng = probe_engine(job, resp)

    with pytest.raises(RuntimeError):
        eng.probe()


def test_probe_blocks_after_a_redirect_to_a_login_page(
    probe_engine, fake_response, make_job, tmp_path
):
    """The clicked URL ended .zip, but we landed on a page — judge the landing."""
    job = make_job(
        url="https://site.test/files/pack.zip",
        filename="pack.zip",
        save_path=str(tmp_path / "pack.zip"),
    )
    resp = fake_response(
        url="https://site.test/accounts/signin", headers={"Content-Type": "text/html"}
    )
    eng = probe_engine(job, resp)

    with pytest.raises(RuntimeError):
        eng.probe()


def test_probe_marks_the_job_failed_through_run(probe_engine, html_response, make_job, tmp_path):
    """run() must surface the guard as a failure, not a silent 0-byte success."""
    job = make_job(filename="download", save_path=str(tmp_path / "download"))
    eng = probe_engine(job, html_response())
    eng._stop = False
    eng.cancel_check = lambda: False

    eng.run()

    assert job.status == DownloadStatus.FAILED
    assert HTML_BLOCK_HINT in job.error.lower()
    assert not list(tmp_path.iterdir()), "nothing may be written for a blocked page"


# --------------------------------------------------------------------------
# probe(): the three cases that must still be allowed through
# --------------------------------------------------------------------------
def test_probe_allows_an_html_url(probe_engine, fake_response, make_job, tmp_path):
    job = make_job(filename="article.html", save_path=str(tmp_path / "article.html"))
    resp = fake_response(
        url="https://site.test/blog/article.html", headers={"Content-Type": "text/html"}
    )
    eng = probe_engine(job, resp)

    eng.probe()

    assert job.filename == "article.html"


def test_probe_allows_an_html_url_with_a_junk_local_name(
    probe_engine, fake_response, make_job, tmp_path
):
    job = make_job(filename="view", save_path=str(tmp_path / "view"))
    resp = fake_response(
        url="https://site.test/docs/page.htm", headers={"Content-Type": "text/html"}
    )
    eng = probe_engine(job, resp)

    eng.probe()

    assert job.filename == "page.htm", "the URL's own name should be adopted"


def test_probe_allows_an_explicit_attachment(probe_engine, fake_response, make_job, tmp_path):
    """Servers mislabel downloads as text/html; 'attachment' is an explicit override."""
    job = make_job(filename="download", save_path=str(tmp_path / "download"))
    resp = fake_response(
        url="https://site.test/get?id=7",
        headers={
            "Content-Type": "text/html; charset=utf-8",
            "Content-Disposition": 'attachment; filename="setup.exe"',
        },
    )
    eng = probe_engine(job, resp)

    eng.probe()

    assert job.filename == "setup.exe"
    assert job.save_path == str(tmp_path / "setup.exe")


def test_probe_allows_a_bare_attachment_without_renaming_to_html(
    probe_engine, fake_response, make_job, tmp_path
):
    job = make_job(filename="download", save_path=str(tmp_path / "download"))
    resp = fake_response(
        url="https://site.test/get?id=7",
        headers={"Content-Type": "text/html", "Content-Disposition": "attachment"},
    )
    eng = probe_engine(job, resp)

    eng.probe()

    assert job.filename == "download", "no name to adopt, and html must not supply one"


def test_probe_allows_a_server_named_html_file(probe_engine, fake_response, make_job, tmp_path):
    job = make_job(filename="download", save_path=str(tmp_path / "download"))
    resp = fake_response(
        url="https://site.test/export?id=3",
        headers={
            "Content-Type": "text/html",
            "Content-Disposition": 'inline; filename="invoice.html"',
        },
    )
    eng = probe_engine(job, resp)

    eng.probe()

    assert job.filename == "invoice.html"


# --------------------------------------------------------------------------
# probe(): ordinary (non-HTML) naming
# --------------------------------------------------------------------------
def test_probe_names_a_junk_job_from_the_disposition(
    probe_engine, fake_response, make_job, tmp_path
):
    job = make_job(
        filename="a5de8a50-7e22-4163-97a4-914d6644caa7",
        save_path=str(tmp_path / "a5de8a50-7e22-4163-97a4-914d6644caa7"),
    )
    resp = fake_response(
        url="https://cdn.test/a5de8a50-7e22-4163-97a4-914d6644caa7",
        headers={
            "Content-Type": "video/mp4",
            "Content-Disposition": "attachment; filename*=UTF-8''Holiday%20clip.mp4",
        },
    )
    eng = probe_engine(job, resp)

    eng.probe()

    assert job.filename == "Holiday clip.mp4"
    assert job.save_path == str(tmp_path / "Holiday clip.mp4")


def test_probe_names_a_junk_job_from_the_final_url(probe_engine, fake_response, make_job, tmp_path):
    job = make_job(
        url="https://short.test/d?id=1", filename="d", save_path=str(tmp_path / "d")
    )
    resp = fake_response(
        url="https://cdn.test/pub/ubuntu-24.04.iso",
        headers={"Content-Type": "application/octet-stream", "Content-Length": "500"},
    )
    eng = probe_engine(job, resp)

    eng.probe()

    assert job.url == "https://cdn.test/pub/ubuntu-24.04.iso", "redirect target is adopted"
    assert job.filename == "ubuntu-24.04.iso"


def test_probe_extends_a_junk_name_with_the_content_type(
    probe_engine, fake_response, make_job, tmp_path
):
    job = make_job(url="https://v.test/watch?v=abc", filename="clip", save_path=str(tmp_path / "clip"))
    resp = fake_response(url="https://v.test/watch?v=abc", headers={"Content-Type": "video/mp4"})
    eng = probe_engine(job, resp)

    eng.probe()

    assert job.filename == "clip.mp4"


def test_probe_keeps_a_good_name_the_user_chose(probe_engine, fake_response, make_job, tmp_path):
    job = make_job(filename="My Movie.mp4", save_path=str(tmp_path / "My Movie.mp4"))
    resp = fake_response(
        url="https://cdn.test/x/9f.mp4",
        headers={
            "Content-Type": "video/mp4",
            "Content-Disposition": 'attachment; filename="tracking-name.mp4"',
        },
    )
    eng = probe_engine(job, resp)

    eng.probe()

    assert job.filename == "My Movie.mp4"
    assert job.save_path == str(tmp_path / "My Movie.mp4")


def test_probe_dedupes_against_an_existing_file(probe_engine, fake_response, make_job, tmp_path):
    (tmp_path / "setup.exe").write_bytes(b"already here")
    job = make_job(filename="download", save_path=str(tmp_path / "download"))
    resp = fake_response(
        url="https://site.test/get",
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": 'attachment; filename="setup.exe"',
        },
    )
    eng = probe_engine(job, resp)

    eng.probe()

    assert job.save_path == str(tmp_path / "setup (1).exe")
    assert (tmp_path / "setup.exe").read_bytes() == b"already here", "must not clobber"


def test_probe_sanitizes_a_traversing_server_name(probe_engine, fake_response, make_job, tmp_path):
    job = make_job(filename="download", save_path=str(tmp_path / "download"))
    resp = fake_response(
        url="https://site.test/get",
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": 'attachment; filename="../../../evil.exe"',
        },
    )
    eng = probe_engine(job, resp)

    eng.probe()

    assert "/" not in job.filename and os.sep not in job.filename
    assert Path(job.save_path).parent == tmp_path, "a download may not escape its folder"


def test_probe_records_the_server_metadata(probe_engine, fake_response, make_job, tmp_path):
    job = make_job(filename="pack.zip", save_path=str(tmp_path / "pack.zip"))
    resp = fake_response(
        url="https://cdn.test/pack.zip",
        headers={
            "Content-Type": "application/zip",
            "Content-Length": "4096",
            "Accept-Ranges": "bytes",
            "ETag": '"abc123"',
            "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT",
        },
    )
    eng = probe_engine(job, resp)

    eng.probe()

    assert job.total_size == 4096
    assert job.supports_ranges is True
    assert job.etag == '"abc123"'
    assert job.last_modified == "Wed, 01 Jan 2025 00:00:00 GMT"


def test_probe_honours_accept_ranges_none(probe_engine, fake_response, make_job, tmp_path):
    job = make_job(filename="pack.zip", save_path=str(tmp_path / "pack.zip"))
    resp = fake_response(
        url="https://cdn.test/pack.zip",
        headers={
            "Content-Type": "application/zip",
            "Content-Length": "4096",
            "Accept-Ranges": "none",
        },
    )
    eng = probe_engine(job, resp)

    eng.probe()

    assert job.supports_ranges is False
