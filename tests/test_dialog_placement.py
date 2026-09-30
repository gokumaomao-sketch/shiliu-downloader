"""Dialogs must open on the monitor the app is on.

Every dialog centred itself on the *screen* rather than on its parent window, so
with the app on a second display, Add download / Options / About / the progress
window / the browser-capture prompt all appeared on the other monitor. The
capture prompt is the worst of them: it is modal and grabs focus, so the click
the user was about to make lands on a window that jumped screens.

These tests drive the real placement helpers with a fake monitor layout rather
than requiring two physical displays.
"""

from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from magic_downloader.gui import dialogs  # noqa: E402


@pytest.fixture(scope="module")
def root():
    """The one Tk root for the whole session.

    A second Tk() in the same process makes Tk fail in unrelated ways later
    ("image doesn't exist" and friends) — test_gui.py already creates one at
    import time, so reuse whatever exists and only create (and destroy) a root
    when there is none. Destroying a borrowed root would break the module that
    owns it.
    """
    existing = getattr(tk, "_default_root", None)
    if existing is not None:
        yield existing
        return
    try:
        r = tk.Tk()
    except Exception:  # pragma: no cover - headless CI
        pytest.skip("no Tk display", allow_module_level=True)
    r.withdraw()
    yield r
    try:
        r.destroy()
    except tk.TclError:
        pass


@pytest.fixture
def secondary_monitor(monkeypatch):
    """A 1920x1080 display sitting to the RIGHT of the primary one."""
    area = (1920, 0, 3840, 1040)          # left, top, right, bottom (work area)
    monkeypatch.setattr(dialogs, "_work_area", lambda widget: area)
    return area


@pytest.fixture
def left_monitor(monkeypatch):
    """A display to the LEFT of the primary one — negative coordinates."""
    area = (-1920, 0, 0, 1040)
    monkeypatch.setattr(dialogs, "_work_area", lambda widget: area)
    return area


def _dialog(root, w=400, h=300):
    win = tk.Toplevel(root)
    win.geometry(f"{w}x{h}")
    win.update_idletasks()
    return win


def _pos(win):
    win.update_idletasks()
    geo = win.geometry()                   # "WxH+X+Y"
    _, _, x, y = geo.replace("x", "+").split("+")[:4] if geo.count("+") >= 2 else (0, 0, 0, 0)
    return int(x), int(y)


@pytest.mark.gui
def test_a_dialog_is_centred_on_its_parent_not_the_monitor(root, secondary_monitor, monkeypatch):
    """Pins the exact position, so it fails if the parent is ignored.

    (An earlier version only asserted "x >= 1920", which the fake monitor
    guaranteed anyway — mutation testing showed it passed even with
    parent-awareness removed.)
    """
    win = _dialog(root)
    px, py, pw, ph = 2200, 300, 1200, 700
    monkeypatch.setattr(dialogs, "_parent_rect", lambda w: (px, py, pw, ph))

    dialogs._place(win, 400, 300)

    x, y = _pos(win)
    assert (x, y) == (px + (pw - 400) // 2, py + (ph - 300) // 2)
    # ...and that is NOT where centring on the monitor would have put it.
    left, top, right, bottom = secondary_monitor
    assert x != left + (right - left - 400) // 2


@pytest.mark.gui
def test_a_dialog_is_kept_inside_the_work_area(root, secondary_monitor, monkeypatch):
    """A parent near the right edge must not push the dialog off-screen."""
    win = _dialog(root)
    monkeypatch.setattr(dialogs, "_parent_rect", lambda w: (3700, 900, 120, 120))

    dialogs._place(win, 400, 300)

    x, y = _pos(win)
    assert x + 400 <= 3840, "the dialog hung off the right edge"
    assert y + 300 <= 1040, "the dialog was placed under the taskbar"


@pytest.mark.gui
def test_a_monitor_left_of_the_primary_works(root, left_monitor, monkeypatch):
    """Negative coordinates are legal; clamping at 0 dragged dialogs to screen 1."""
    win = _dialog(root)
    monkeypatch.setattr(dialogs, "_parent_rect", lambda w: (-1500, 200, 1000, 700))

    dialogs._place(win, 400, 300)

    x, _y = _pos(win)
    assert x < 0, "a display left of the primary has negative x"
    assert x >= -1920


@pytest.mark.gui
def test_without_a_usable_parent_it_centres_on_the_monitor(root, secondary_monitor, monkeypatch):
    win = _dialog(root)
    monkeypatch.setattr(dialogs, "_parent_rect", lambda w: None)

    dialogs._place(win, 400, 300)

    x, y = _pos(win)
    assert x == 1920 + (1920 - 400) // 2
    assert y == (1040 - 300) // 2


@pytest.mark.gui
def test_a_minimised_parent_is_not_used_as_an_anchor(root):
    """Windows reports ~-32000 for an iconified window."""
    parent = tk.Toplevel(root)
    parent.geometry("600x400")
    parent.update_idletasks()
    parent.iconify()
    parent.update_idletasks()
    win = tk.Toplevel(parent)

    assert dialogs._parent_rect(win) is None, (
        "centring on a minimised window puts the dialog off-screen"
    )
    parent.destroy()


@pytest.mark.gui
def test_a_hidden_parent_is_not_used_as_an_anchor(root):
    """The app can sit withdrawn in the tray while a progress window opens."""
    parent = tk.Toplevel(root)
    parent.geometry("600x400")
    parent.update_idletasks()
    parent.withdraw()
    win = tk.Toplevel(parent)

    assert dialogs._parent_rect(win) is None
    parent.destroy()


@pytest.mark.gui
def test_a_normal_parent_is_used_as_an_anchor(root):
    parent = tk.Toplevel(root)
    parent.geometry("600x400+120+80")
    parent.deiconify()
    parent.update_idletasks()
    win = tk.Toplevel(parent)

    rect = dialogs._parent_rect(win)

    assert rect is not None
    assert rect[2] > 1 and rect[3] > 1
    parent.destroy()


@pytest.mark.gui
def test_fit_center_also_lands_on_the_parents_monitor(root, secondary_monitor, monkeypatch):
    win = tk.Toplevel(root)
    tk.Label(win, text="x" * 40).pack()
    monkeypatch.setattr(dialogs, "_parent_rect", lambda w: (2200, 300, 1200, 700))

    dialogs._fit_center(win, 300, 200)

    x, _y = _pos(win)
    assert x >= 1920, "_fit_center must follow the parent too"
    win.destroy()


@pytest.mark.gui
def test_work_area_returns_a_sane_rectangle(root):
    """The real helper, on whatever display this machine actually has."""
    left, top, right, bottom = dialogs._work_area(root)

    assert right > left and bottom > top
    assert right - left >= 640 and bottom - top >= 480
