"""GUI tests — window sizing, dialog layout and the Options round-trip.

Everything here drives real Tk widgets; nothing is stubbed except the parts that
would touch the network, the registry or the system tray.

Two deliberate techniques are used repeatedly:

* **Geometry spying.** ``wm geometry`` *reads back* the window's current size,
  not the size that was requested — for an unmapped window that is still the
  content's requested size, and for a mapped one the window manager may have
  clamped it. So the assertions record the geometry string the code under test
  actually asks for. That string is the whole observable contract of
  ``_fit_center`` (and, for AboutDialog, the thing that must never appear).
* **One Tk root for the module.** Images and Tk variables belong to the
  interpreter of the root that created them; several roots in one process make
  widgets fail with `image "pyimage1" doesn't exist`.
"""

from __future__ import annotations

import contextlib
import re

import pytest

tk = pytest.importorskip("tkinter")
ttk = pytest.importorskip("tkinter.ttk")

try:
    _root = tk.Tk()
    _root.withdraw()
except Exception:  # pragma: no cover - headless CI
    pytest.skip("no Tk display", allow_module_level=True)

# Every test in this module needs a display.
pytestmark = pytest.mark.gui

GEOMETRY_RE = re.compile(r"^(?:(\d+)x(\d+))?(?:([+-]\d+)([+-]\d+))?$")


# ── helpers ─────────────────────────────────────────────────────────────


def parse_geometry(spec: str) -> tuple[int | None, int | None, int | None, int | None]:
    """Split a Tk geometry string into (width, height, x, y).

    Either half may be absent: ``"+100+80"`` only moves a window and ``"660x560"``
    only sizes it. Width/height being None is exactly what "this dialog does not
    pin its size" looks like.
    """
    m = GEOMETRY_RE.match(spec)
    assert m is not None, f"unparseable geometry {spec!r}"
    return tuple(int(g) if g is not None else None for g in m.groups())  # type: ignore[return-value]


@contextlib.contextmanager
def geometry_spy(cls):
    """Record every geometry string *cls* instances request, and forward it."""
    calls: list[str] = []
    had_own = "geometry" in cls.__dict__
    previous = cls.__dict__.get("geometry")

    def geometry(self, newGeometry=None):  # noqa: N803 - Tk's parameter name
        if newGeometry is not None:
            calls.append(newGeometry)
        return tk.Wm.wm_geometry(self, newGeometry)

    cls.geometry = geometry
    try:
        yield calls
    finally:
        if had_own:
            cls.geometry = previous
        else:
            del cls.geometry


def close(win) -> None:
    with contextlib.suppress(tk.TclError):
        win.grab_release()
    with contextlib.suppress(tk.TclError):
        win.destroy()


def button_texts(parent) -> set[str]:
    return {
        c.cget("text")
        for c in parent.winfo_children()
        if isinstance(c, (ttk.Button, tk.Button))
    }


def find_bar_with_button(win, label: str):
    """The direct pack child of *win* that holds a button labelled *label*."""
    for child in win.pack_slaves():
        if label in button_texts(child):
            return child
    return None


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(scope="module", autouse=True)
def _module_root():
    yield _root
    close(_root)


@pytest.fixture
def root():
    """The shared root, with every window a test opened cleaned up after it."""
    yield _root
    for child in list(_root.winfo_children()):
        close(child)


@pytest.fixture(autouse=True)
def no_registry_writes(monkeypatch):
    """SettingsDialog._save() writes the HKCU "Run" key for real. Not in tests.

    Returns the list of values passed to ``startup.set_enabled``.
    """
    from magic_downloader import startup

    calls: list[bool] = []
    monkeypatch.setattr(startup, "set_enabled", lambda enable: calls.append(bool(enable)))
    monkeypatch.setattr(startup, "is_enabled", lambda: False)
    return calls


@pytest.fixture
def sized_window(root):
    """Factory: a withdrawn Toplevel whose content requests an exact size."""
    made = []

    def _make(width: int, height: int):
        win = tk.Toplevel(root)
        win.withdraw()
        tk.Frame(win, width=width, height=height).pack()
        made.append(win)
        return win

    yield _make
    for win in made:
        close(win)


@pytest.fixture
def open_settings(root, settings):
    """Factory: a live SettingsDialog plus the dict its Save button produced."""
    from magic_downloader.gui.dialogs import SettingsDialog

    made = []

    def _open(overrides: dict | None = None):
        values = dict(settings)
        values.update(overrides or {})
        saved: dict = {}
        dlg = SettingsDialog(root, values, saved.update)
        made.append(dlg)
        return dlg, saved

    yield _open
    for dlg in made:
        close(dlg)


@pytest.fixture
def app(data_dir, monkeypatch):
    """The real main window, minus the tray icon and the localhost API server.

    ``_load_brand_image`` is stubbed too: it builds a PhotoImage on the *default*
    root, and the app is a second Tk root, so the toolbar labels would fail with
    `image "pyimage1" doesn't exist`. That is an artifact of running two roots in
    one test process, not app behaviour — the source already treats a missing
    brand image as a supported fallback.
    """
    from magic_downloader.gui.app import MagicDownloaderApp

    monkeypatch.setattr(MagicDownloaderApp, "_setup_tray", lambda self: None)
    monkeypatch.setattr(MagicDownloaderApp, "_start_browser_server", lambda self: None)
    monkeypatch.setattr(MagicDownloaderApp, "_load_brand_image", lambda self, *a, **k: None)

    win = MagicDownloaderApp()
    win.withdraw()
    try:
        yield win
    finally:
        with contextlib.suppress(Exception):
            win.manager.shutdown()
        close(win)


# ── _fit_center ─────────────────────────────────────────────────────────


def test_fit_center_sizes_the_window_to_its_content(sized_window):
    from magic_downloader.gui.dialogs import _fit_center

    win = sized_window(420, 300)
    with geometry_spy(tk.Toplevel) as calls:
        _fit_center(win, 200, 100)

    assert len(calls) == 1
    w, h, _x, _y = parse_geometry(calls[0])
    assert (w, h) == (win.winfo_reqwidth(), win.winfo_reqheight()) == (420, 300)


def test_fit_center_never_goes_below_the_given_minimums(sized_window):
    from magic_downloader.gui.dialogs import _fit_center

    win = sized_window(100, 60)
    with geometry_spy(tk.Toplevel) as calls:
        _fit_center(win, 400, 320)

    w, h, _x, _y = parse_geometry(calls[0])
    assert (w, h) == (400, 320)


def test_fit_center_grows_past_the_minimum_for_tall_content(sized_window):
    """The Options regression: a tab taller than the designed height must not be
    clipped, or its Save button ends up off-screen under display scaling."""
    from magic_downloader.gui.dialogs import _fit_center

    win = sized_window(300, 900)
    with geometry_spy(tk.Toplevel) as calls:
        _fit_center(win, 620, 560)

    w, h, _x, _y = parse_geometry(calls[0])
    assert w == 620, "the minimum width is a floor, not a ceiling"
    assert h == 900, "content taller than the minimum must not be cut off"


def test_fit_center_stays_within_the_screen(sized_window):
    """Sizes are clamped to the WORK AREA of the monitor the dialog is on —
    not the raw screen, so a dialog never ends up under the taskbar."""
    from magic_downloader.gui.dialogs import _fit_center, _work_area

    win = sized_window(20000, 20000)
    left, top, right, bottom = _work_area(win)
    with geometry_spy(tk.Toplevel) as calls:
        _fit_center(win, 100, 100)

    w, h, x, y = parse_geometry(calls[0])
    assert w == (right - left) - 60
    assert h == (bottom - top) - 100
    assert x >= left and y >= top, "an oversized window must not be positioned off-screen"


def test_fit_center_centers_the_window(sized_window):
    """With no usable parent, centre within the monitor's work area."""
    from magic_downloader.gui.dialogs import _fit_center, _work_area

    win = sized_window(400, 240)
    left, top, right, bottom = _work_area(win)
    with geometry_spy(tk.Toplevel) as calls:
        _fit_center(win)

    w, h, x, y = parse_geometry(calls[0])
    assert x == left + (right - left - w) // 2
    assert y == top + (bottom - top - h) // 2


# ── SettingsDialog: layout ──────────────────────────────────────────────


def test_settings_dialog_has_every_tab(open_settings):
    dlg, _saved = open_settings()
    notebook = next(c for c in dlg.pack_slaves() if isinstance(c, ttk.Notebook))

    labels = [notebook.tab(t, "text").strip() for t in notebook.tabs()]
    assert labels == ["常规", "连接", "文件类型", "视频 / ffmpeg", "浏览器", "站点归档"]


def test_settings_button_bar_is_reserved_at_the_bottom(open_settings):
    """The Save/Cancel bar is packed BOTTOM *before* the notebook, so however
    tall a tab gets, the notebook loses the space and the buttons never do."""
    dlg, _saved = open_settings()
    bar = find_bar_with_button(dlg, "保存")
    assert bar is not None, "no Save button found on a direct child of the dialog"
    assert button_texts(bar) == {"保存", "取消"}

    notebook = next(c for c in dlg.pack_slaves() if isinstance(c, ttk.Notebook))
    assert bar.pack_info()["side"] == "bottom"
    assert int(bar.pack_info()["expand"]) == 0
    assert int(notebook.pack_info()["expand"]) == 1
    order = dlg.pack_slaves()
    assert order.index(bar) < order.index(notebook)


# ── SettingsDialog: _save ───────────────────────────────────────────────


def test_settings_save_writes_the_video_options(open_settings):
    dlg, saved = open_settings({"stream_output_ts": False, "prefer_smaller_files": False})

    dlg.ts_out_var.set(True)
    dlg.smaller_var.set(True)
    dlg.quality_var.set("1080p")
    dlg._save()

    assert saved["stream_output_ts"] is True
    assert saved["prefer_smaller_files"] is True
    assert saved["default_video_quality"] == "1080"


def test_settings_video_quality_round_trips_through_its_label(open_settings):
    """The combobox shows labels but the setting stores values; a saved dialog
    that was never touched must not silently rewrite the user's choice."""
    dlg, saved = open_settings(
        {"default_video_quality": "720", "stream_output_ts": True, "prefer_smaller_files": True}
    )

    assert dlg.quality_var.get() == "720p"
    assert dlg.ts_out_var.get() is True
    assert dlg.smaller_var.get() is True

    dlg._save()
    assert saved["default_video_quality"] == "720"
    assert saved["stream_output_ts"] is True
    assert saved["prefer_smaller_files"] is True


def test_settings_save_clamps_out_of_range_numbers(open_settings):
    dlg, saved = open_settings()

    dlg.conn_var.set(999)
    dlg.max_var.set(0)
    dlg.workers_var.set(-4)
    dlg.speed_var.set(-100)
    dlg.timeout_var.set(1)
    dlg.retries_var.set(99)
    dlg.chunk_var.set(512)          # KB in the UI, bytes in the settings
    dlg.port_var.set(80)
    dlg._save()

    assert saved["connections"] == 32
    assert saved["max_simultaneous"] == 1
    assert saved["media_workers"] == 1
    assert saved["max_speed_kbps"] == 0
    assert saved["timeout"] == 5
    assert saved["retries"] == 15
    assert saved["chunk_size"] == 512 * 1024
    assert saved["browser_port"] == 1024


def test_settings_save_keeps_the_user_agent_when_the_field_is_blanked(open_settings):
    """An empty User-Agent box means "leave it alone", not "send no UA"."""
    dlg, saved = open_settings({"user_agent": "KeepMe/1.0"})

    dlg.ua_var.set("   ")
    dlg._save()

    assert saved["user_agent"] == "KeepMe/1.0"


def test_settings_save_writes_the_general_and_browser_flags(open_settings):
    dlg, saved = open_settings()

    dlg.confirm_delete.set(False)
    dlg.browser_auto.set(False)
    dlg.close_to_tray.set(False)
    dlg.minimize_to_tray.set(True)
    dlg.browser_on.set(False)
    dlg.confirm_captures.set(False)
    dlg.token_var.set("  s3cret  ")
    dlg.path_var.set("  C:/tmp/dl  ")
    dlg._save()

    assert saved["confirm_delete"] is False
    assert saved["browser_auto_start"] is False
    assert saved["close_to_tray"] is False
    assert saved["minimize_to_tray"] is True
    assert saved["browser_integration"] is False
    assert saved["confirm_browser_captures"] is False
    assert saved["browser_token"] == "s3cret"
    assert saved["default_save_path"] == "C:/tmp/dl"


def test_settings_save_applies_the_run_at_startup_checkbox(open_settings, no_registry_writes):
    dlg, _saved = open_settings()

    dlg.startup_var.set(True)
    dlg._save()

    assert no_registry_writes == [True]


def test_settings_file_types_tab_normalises_extensions(open_settings, tmp_path):
    """Extensions typed without a dot (or comma separated) still map correctly."""
    dlg, saved = open_settings()

    index = dlg._all_categories().index("Video")
    dlg.cat_listbox.selection_clear(0, tk.END)
    dlg.cat_listbox.selection_set(index)
    dlg._on_cat_select()

    dlg.ext_entry.delete("1.0", tk.END)
    dlg.ext_entry.insert("1.0", "mkv, MP4  .avi")
    dlg.cat_folder_var.set(str(tmp_path / "Movies"))
    dlg._save()

    assert saved["category_extensions"]["Video"] == [".mkv", ".mp4", ".avi"]
    assert saved["category_paths"]["Video"] == str(tmp_path / "Movies")


def test_settings_save_calls_back_once_and_closes_the_dialog(open_settings):
    from magic_downloader.gui.dialogs import SettingsDialog

    calls = []
    dlg = SettingsDialog(_root, {}, calls.append)
    try:
        dlg._save()
        assert len(calls) == 1
        assert calls[0] is dlg.settings
        assert not dlg.winfo_exists()
    finally:
        close(dlg)


# ── AboutDialog ─────────────────────────────────────────────────────────


def test_about_dialog_never_pins_its_size(root):
    """It only positions itself. A fixed WxH froze the window at its pre-check
    height and hid the "Install now" button that a check adds."""
    from magic_downloader.gui.dialogs import AboutDialog

    with geometry_spy(AboutDialog) as calls:
        dlg = AboutDialog(root, "1.2.3")
        try:
            sizes = [parse_geometry(c)[:2] for c in calls]
            assert calls, "the dialog never positioned itself"
            assert all(size == (None, None) for size in sizes), (
                f"AboutDialog pinned its geometry: {calls}"
            )
        finally:
            close(dlg)


def test_about_dialog_has_no_upstream_update_controls(root):
    from magic_downloader.gui.dialogs import AboutDialog

    dlg = AboutDialog(root, "1.2")
    try:
        labels = [w.cget("text") for w in dlg.winfo_children()
                  if isinstance(w, tk.Label)]
        assert not hasattr(dlg, "update_btn")
        assert not hasattr(dlg, "check_btn")
        assert not hasattr(dlg, "_do_update")
    finally:
        close(dlg)

# ── main window ─────────────────────────────────────────────────────────


def test_main_window_reserves_the_status_bar_before_the_body(app):
    """The status bar is packed BOTTOM first; the body expands into what's left.

    Packed the other way round the expanding body claims the cavity and the
    status strip is the one that gets squeezed out when the window is short.
    """
    slaves = app.pack_slaves()
    assert app._status_frame in slaves and app._body in slaves

    assert app._status_frame.pack_info()["side"] == "bottom"
    assert int(app._status_frame.pack_info()["expand"]) == 0
    assert slaves.index(app._status_frame) < slaves.index(app._body)

    body_info = app._body.pack_info()
    assert int(body_info["expand"]) == 1
    assert body_info["fill"] == "both"


def test_main_window_status_bar_keeps_its_height(app):
    """pack_propagate(False) is what stops the labels inside from resizing it."""
    app.update_idletasks()
    bar = app._status_frame
    # Tk's own reading: 0 = the children do not drive the frame's size.
    # (Misc.pack_propagate() answers None rather than False for 0, so ask Tcl.)
    assert int(app.tk.call("pack", "propagate", bar._w)) == 0
    assert bar.winfo_reqheight() == 36
    assert bar.winfo_reqheight() > max(c.winfo_reqheight() for c in bar.winfo_children())


def test_main_window_dims_actions_that_do_not_apply(app, make_job):
    from magic_downloader.models import DownloadStatus
    from magic_downloader.gui import theme as T

    def fg(key: str) -> str:
        return app._buttons[key].text_lbl.cget("fg")

    # Nothing selected: every selection-dependent action is dimmed.
    for key in ("resume", "pause", "stop", "delete", "open"):
        assert fg(key) == T.FG_ON_DARK_DISABLED, f"{key} is lit with no selection"
    for key in ("options",):
        assert fg(key) == T.FG_ON_DARK, f"{key} should always be available"

    job = make_job(filename="done.zip", status=DownloadStatus.COMPLETE, total_size=10, downloaded=10)
    app.manager.jobs.append(job)
    app._refresh_all()
    app.tree.selection_set(job.id)
    app._update_toolbar_state()

    assert fg("open") == T.FG_ON_DARK, "a finished download can be opened"
    assert fg("delete") == T.FG_ON_DARK
    assert fg("resume") == T.FG_ON_DARK_DISABLED, "a finished download cannot be resumed"
    assert fg("stop") == T.FG_ON_DARK_DISABLED, "a finished download cannot be stopped"


def test_progress_details_open_only_when_selected(app, make_job, monkeypatch):
    from magic_downloader.models import DownloadStatus
    import magic_downloader.gui.app as app_module

    job = make_job(filename="done.mp4", status=DownloadStatus.COMPLETE)
    app.manager.jobs.append(job)
    app.tree.insert("", "end", iid=job.id, values=(job.filename,))
    app.tree.selection_set(job.id)
    opened = []
    monkeypatch.setattr(app_module, "DownloadProgressDialog", lambda *a, **kw: opened.append(job.id) or object())
    app.manager.settings["show_progress_dialog"] = False  # older settings must not block a click
    assert not opened
    app._on_double_click()
    assert opened == [job.id]


def test_main_window_status_line_summarises_the_queue(app, make_job):
    from magic_downloader.models import DownloadStatus

    app.manager.jobs.extend(
        [
            make_job(filename="a.bin", status=DownloadStatus.DOWNLOADING, speed_bps=1024.0),
            make_job(filename="b.bin", status=DownloadStatus.COMPLETE),
            make_job(filename="c.bin", status=DownloadStatus.QUEUED),
        ]
    )
    app._update_status()

    assert app.status_var.get() == (
        "总任务：3   ·   进行中：1   ·   已完成：1   ·   同时下载："
        f"{app.manager.settings.get('max_simultaneous', 3)}"
    )
    assert app.speed_badge.cget("text") == "1.0 KB/s"
    assert "浏览器服务未启动" in app.browser_badge.cget("text")


def test_v2_navigation_selection_and_download_actions(app, tmp_path, monkeypatch):
    """Real widgets + real localhost download, isolated from user tasks/files."""
    import hashlib
    from pathlib import Path
    import threading
    import time
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from magic_downloader.gui.dialogs import AddDownloadDialog
    from magic_downloader.models import DownloadStatus as S
    import magic_downloader.gui.app as app_module

    payload = bytes(range(256)) * 32768
    (tmp_path / "payload.bin").write_bytes(payload)
    downloads = tmp_path / "result"
    downloads.mkdir()

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args): pass
        def copyfile(self, source, output):
            try:
                while chunk := source.read(65536):
                    output.write(chunk); output.flush(); time.sleep(.015)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    app.manager.settings["connections"] = 1
    app.manager.settings["confirm_delete"] = False
    opened = []
    monkeypatch.setattr(app, "_open_path", opened.append)
    monkeypatch.setattr(app_module.messagebox, "ask", lambda *a, **k: "purge")
    app.deiconify(); app.update()

    def wait_for(predicate, timeout=12):
        deadline = time.monotonic() + timeout
        expired, errors = [], []
        def poll():
            try:
                if predicate(): app.quit()
                elif time.monotonic() >= deadline: expired.append(True); app.quit()
                else: app.after(20, poll)
            except Exception as exc:
                errors.append(exc); app.quit()
        app.after(0, poll); app.mainloop()
        if errors: raise errors[0]
        assert not expired, "localhost GUI workflow timed out"

    def add(name):
        app._buttons["add"]._click()
        dialog = next(w for w in app.winfo_children() if isinstance(w, AddDownloadDialog))
        dialog.url_var.set(f"http://127.0.0.1:{server.server_port}/payload.bin")
        dialog.name_var.set(name); dialog.path_var.set(str(downloads)); dialog.conn_var.set(1)
        dialog._submit()
        return app.manager.jobs[0]

    try:
        job = add("ui-check.bin")
        wait_for(lambda: job.downloaded > 0)
        app._refresh_all(); app.update()
        # Checkbox selection on the painted row must select the native task ID.
        row = app.task_rows.rows[job.id]
        row.event_generate("<Button-1>", x=16, y=29); app.update()
        assert app._selected_ids() == [job.id]
        app._buttons["pause"]._click()
        assert job.status == S.PAUSED
        app._refresh_all()
        assert app._buttons["resume"]._enabled and not app._buttons["pause"]._enabled
        app._buttons["resume"]._click()
        wait_for(lambda: job.status == S.COMPLETE)
        assert hashlib.sha256(Path(job.save_path).read_bytes()).digest() == hashlib.sha256(payload).digest()
        app._refresh_all(); app.update()
        app._buttons["open"]._click(); app._buttons["folder"]._click()
        assert opened == [Path(job.save_path), Path(job.save_path).parent]
        app.search_var.set("does-not-match"); app.update()
        assert not app.tree.get_children()
        app.search_var.set("UI-CHECK"); app.update()
        assert app.tree.get_children() == (job.id,)
        app.search_var.set(""); app.update()
        complete_index = next(i for i,(_,key) in enumerate(app._sidebar_items) if key == "complete")
        app.cat_list.selection_set(complete_index); app._on_category_select()
        assert app.tree.get_children() == (job.id,)
        document_index = next(i for i,(_,key) in enumerate(app._sidebar_items) if key == "cat:Documents")
        app.cat_list.selection_set(document_index); app._on_category_select()
        assert not app.tree.get_children()
        app.cat_list.selection_set(0); app._on_category_select(); app.update()
        app.tree.selection_set(job.id); app._update_toolbar_state()
        app._buttons["delete"]._click()
        assert app.manager.get_job(job.id) is None and Path(job.save_path).exists()
        cancelled = add("ui-stop.bin")
        wait_for(lambda: cancelled.downloaded > 0)
        app._refresh_all(); app.tree.selection_set(cancelled.id); app._update_toolbar_state()
        app._buttons["stop"]._click()
        assert cancelled.status == S.CANCELLED
        wait_for(lambda: cancelled.id not in app.manager._threads or not app.manager._threads[cancelled.id].is_alive())
        app._refresh_all(); app.tree.selection_set(cancelled.id); app._update_toolbar_state()
        app._buttons["delete"]._click()
        assert app.manager.get_job(cancelled.id) is None
        assert not Path(cancelled.save_path + ".part").exists()
        assert not app.manager.jobs
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_v21_settings_toolbar_save_reload(app, tmp_path):
    from magic_downloader import config
    from magic_downloader.gui.dialogs import SettingsDialog
    app.deiconify(); app.update()
    app._buttons["options"]._click()
    dialog = next(w for w in app.winfo_children() if isinstance(w, SettingsDialog))
    nb = next(w for w in dialog.winfo_children() if isinstance(w, ttk.Notebook))
    heights = []
    for tab in nb.tabs():
        nb.select(tab); app.update()
        heights.append(dialog.winfo_height())
        assert dialog.nametowidget(tab).winfo_ismapped()
    assert heights[0] < heights[3]  # General should not inherit Video's unused space.
    dialog.path_var.set(str(tmp_path / "自定义保存"))
    dialog.confirm_delete.set(False)
    dialog._save(); app.update()
    saved = config.load_settings()
    assert saved["default_save_path"] == str(tmp_path / "自定义保存")
    assert saved["confirm_delete"] is False
    app._buttons["options"]._click()
    reopened = next(w for w in app.winfo_children() if isinstance(w, SettingsDialog))
    assert reopened.path_var.get() == saved["default_save_path"]
    reopened.destroy()


def test_final_font_tokens_cover_main_and_dialogs(app):
    import ast
    from pathlib import Path
    from tkinter import font
    from magic_downloader.gui import theme as T
    # Page code must not define local font families/sizes or synthetic Bold.
    for path in Path(T.__file__).parent.glob("*.py"):
        if path.name == "theme.py": continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "font":
                assert not isinstance(node.value, (ast.Tuple, ast.List)), str(path)
    tokens = [T.FONT_TITLE, T.FONT_BRAND, T.FONT_BODY, T.FONT_NAV,
              T.FONT_BUTTON, T.FONT_TASK, T.FONT_HEADER, T.FONT_AUX]
    for token in tokens:
        native = font.Font(root=app, font=token)
        assert native.actual("family") == "PingFang SC"
        assert native.actual("size") == token[1]
    assert font.Font(root=app, font=T.FONT_BODY).actual("weight") == "normal"
    assert T.FONT_TITLE[0] == "PingFangSC-Semibold"
    assert T.FONT_TASK[0] == "PingFangSC-Medium"
    heading = font.Font(root=app, font=ttk.Style(app).lookup("Treeview.Heading", "font"))
    assert heading.actual("family") == "PingFang SC"
    assert heading.actual("size") == T.FONT_HEADER[1]
