"""Add download and settings dialogs — polished ."""

from __future__ import annotations

import sys
import threading
import tkinter as tk
from pathlib import Path
from magic_downloader.paths import migrated_path
from tkinter import filedialog, ttk

from magic_downloader.gui import quiet_dialogs as messagebox
from magic_downloader.gui import quiet_dialogs as simpledialog
from typing import Callable
from urllib.parse import urlparse

from magic_downloader.config import default_download_dir, category_for_filename, resolve_save_path, site_folder_for_url
from magic_downloader.engine import suggest_filename
from magic_downloader.gui import theme as T
from magic_downloader.media import ffmpeg as ffmpeg_mod
from magic_downloader.gui.widgets import ProgressBar, SegmentBar  # noqa: F401
from magic_downloader.media.detect import classify_url
from magic_downloader.models import (
    DownloadJob,
    DownloadStatus,
    format_bytes,
    format_eta,
    format_speed,
)

_CATEGORY_CN = {"General": "常规", "Compressed": "压缩文件", "Documents": "文档",
                "Music": "音乐", "Video": "视频"}


def category_label(name: str) -> str:
    return _CATEGORY_CN.get(name, name)


def category_key(label: str) -> str:
    return next((key for key, value in _CATEGORY_CN.items() if value == label), label)


def _work_area(widget: tk.Misc) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) usable area of the monitor holding *widget*.

    Uses the Windows work area so a dialog is never placed under the taskbar.
    Falls back to Tk's idea of the screen — which is the PRIMARY monitor only,
    hence the ctypes path: without it every dialog was positioned as if the app
    were on screen 1.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _RECT(ctypes.Structure):
                _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                            ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

            class _MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT),
                            ("rcWork", _RECT), ("dwFlags", wintypes.DWORD)]

            user32 = ctypes.windll.user32
            hmon = user32.MonitorFromWindow(int(widget.winfo_id()), 2)  # NEAREST
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                r = info.rcWork
                if r.right > r.left and r.bottom > r.top:
                    return r.left, r.top, r.right, r.bottom
        except Exception:  # noqa: BLE001 — any failure just means "use Tk's view"
            pass
    return 0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight()


def _parent_rect(win: tk.Toplevel) -> tuple[int, int, int, int] | None:
    """(x, y, w, h) of the window this dialog belongs to, or None if unusable.

    None when the parent is minimised or hidden: Windows reports coordinates
    near -32000 for an iconified window, and centring on that puts the dialog
    off-screen. That is a real case here — a progress window can be opened from
    the tray while the main window is withdrawn.
    """
    parent = win.master
    if parent is None:
        return None
    try:
        if not parent.winfo_exists():
            return None
        if getattr(parent, "state", lambda: "normal")() in ("iconic", "withdrawn"):
            return None
        x, y = parent.winfo_rootx(), parent.winfo_rooty()
        w, h = parent.winfo_width(), parent.winfo_height()
        if x < -30000 or y < -30000 or w <= 1 or h <= 1:
            return None
        return x, y, w, h
    except tk.TclError:
        return None


def _placement(win: tk.Toplevel, w: int, h: int) -> tuple[int, int]:
    """Top-left for a *w*x*h* dialog: centred on its parent, inside that monitor."""
    rect = _parent_rect(win)
    anchor = win.master if rect is not None else win
    try:
        left, top, right, bottom = _work_area(anchor)
    except tk.TclError:
        left, top, right, bottom = _work_area(win)
    if rect is not None:
        px, py, pw, ph = rect
        x = px + (pw - w) // 2
        y = py + (ph - h) // 2
    else:
        x = left + (right - left - w) // 2
        y = top + (bottom - top - h) // 2
    # A parent near a screen edge would otherwise straddle two monitors.
    return max(left, min(right - w, x)), max(top, min(bottom - h, y))


def _place(win: tk.Toplevel, w: int, h: int) -> None:
    """Centre *win* over its parent, kept inside that monitor's work area."""
    x, y = _placement(win, w, h)
    win.geometry(f"+{x}+{y}")


def _center(win: tk.Toplevel) -> None:
    win.update_idletasks()
    _place(win, win.winfo_width(), win.winfo_height())


def _fit_center(win: tk.Toplevel, min_w: int = 0, min_h: int = 0) -> None:
    """Size *win* to fit its content, then center it.

    Uses the content's requested size, so the window is never too short to show
    its buttons — regardless of display DPI/font scaling (which is what made the
    fixed pixel geometries clip). Never smaller than (min_w, min_h) — the
    designed size acts as a floor — and never larger than the screen.
    """
    win.update_idletasks()
    w = max(min_w, win.winfo_reqwidth())
    h = max(min_h, win.winfo_reqheight())
    rect = _parent_rect(win)
    anchor = win.master if rect is not None else win
    try:
        left, top, right, bottom = _work_area(anchor)
    except tk.TclError:
        left, top, right, bottom = _work_area(win)
    w = min(w, (right - left) - 60)
    h = min(h, (bottom - top) - 100)
    # One geometry call, so the window never visibly resizes and then jumps.
    x, y = _placement(win, w, h)
    win.geometry(f"{w}x{h}+{x}+{y}")


def _dedupe_name(folder: Path, name: str) -> str:
    """Return a non-colliding file name in *folder* by appending (1), (2), …"""
    p = folder / name
    if not p.exists() and not Path(str(p) + ".part").exists():
        return name
    stem, suf = Path(name).stem, Path(name).suffix
    i = 1
    while True:
        cand = f"{stem} ({i}){suf}"
        cp = folder / cand
        if not cp.exists() and not Path(str(cp) + ".part").exists():
            return cand
        i += 1


def resolve_name_conflict(parent: tk.Misc, folder: Path, name: str) -> tuple[str, str]:
    """filename collision. If *name* already exists in *folder*, ask
    the user what to do instead of silently versioning.

    Returns ``(action, final_name)`` where action is one of:
      • ``"ok"``        — no collision, use *name* as-is
      • ``"overwrite"`` — replace the existing file (name unchanged)
      • ``"rename"``    — keep both; *final_name* is a versioned name
      • ``"cancel"``    — user backed out; caller should abort
    """
    p = folder / name
    if not p.exists() and not Path(str(p) + ".part").exists():
        return ("ok", name)
    choice = messagebox.ask(
        "文件已存在",
        f"文件：\n\n    {name}\n\n"
        f"已存在于：\n{folder}\n\n"
        "•  保留两个文件：为新文件名添加序号\n"
        "•  覆盖：替换现有文件\n",
        [("保留两个", "rename"), ("覆盖", "overwrite"), ("取消", "cancel")],
        parent=parent,
    )
    if choice == "overwrite":
        return ("overwrite", name)
    if choice == "rename":
        return ("rename", _dedupe_name(folder, name))
    return ("cancel", name)


def _confirm_create_folder(parent: tk.Misc, folder: str) -> bool:
    """: if the target folder doesn't exist, offer to create it.

    Returns True if the folder exists (or was created), False to abort.
    """
    if not folder:
        messagebox.showerror("保存文件夹", "请选择保存文件夹。", parent=parent)
        return False
    p = migrated_path(folder)
    if p.exists():
        return True
    if not messagebox.askyesno(
        "创建文件夹？",
        f"此文件夹不存在：\n\n{folder}\n\n现在创建吗？",
        parent=parent,
    ):
        return False
    try:
        p.mkdir(parents=True, exist_ok=True)
        return True
    except OSError as exc:
        messagebox.showerror("创建文件夹", f"无法创建文件夹：\n{exc}", parent=parent)
        return False


class AddDownloadDialog(tk.Toplevel):
    def __init__(
        self,
        master: tk.Misc,
        settings: dict,
        on_submit: Callable[[DownloadJob], None],
        initial_url: str = "",
    ) -> None:
        super().__init__(master)
        self.title("新建下载 — 拾流下载器")
        self.settings = settings
        self.on_submit = on_submit
        self.resizable(True, False)
        self.transient(master)
        self.grab_set()
        self.configure(bg=T.BG)
        self.geometry("600x340")
        self.minsize(560, 320)

        header = tk.Frame(self, bg=T.BG_TOOLBAR, height=44)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(
            header,
            text="新建下载",
            bg=T.BG_TOOLBAR,
            fg=T.FG_ON_DARK,
            font=T.FONT_TITLE,
            anchor="w",
        ).pack(fill=tk.BOTH, expand=True, padx=12)

        frm = tk.Frame(self, bg=T.BG, padx=16, pady=9)
        frm.pack(fill=tk.BOTH, expand=True)

        def row_label(r: int, text: str) -> None:
            tk.Label(frm, text=text, bg=T.BG, fg=T.FG, font=T.FONT_UI, anchor="w").grid(
                row=r, column=0, sticky="w", pady=3, padx=(0, 10)
            )

        row_label(0, "下载链接：")
        self.url_var = tk.StringVar(value=initial_url)
        url_entry = ttk.Entry(frm, textvariable=self.url_var, width=64, font=T.FONT_UI)
        url_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=3)
        url_entry.focus_set()

        row_label(1, "文件名：")
        self.name_var = tk.StringVar(value=suggest_filename(initial_url) if initial_url else "")
        ttk.Entry(frm, textvariable=self.name_var, width=48, font=T.FONT_UI).grid(
            row=1, column=1, columnspan=2, sticky="ew", pady=3
        )

        row_label(2, "分类：")
        cats = list(settings.get("category_paths", {}).keys()) or [
            "General",
            "Compressed",
            "Documents",
            "Music",
            "Video",
        ]
        self.cat_var = tk.StringVar(value=category_label("General"))
        ttk.Combobox(
            frm, textvariable=self.cat_var, values=[category_label(c) for c in cats], state="readonly", width=22, font=T.FONT_UI
        ).grid(row=2, column=1, sticky="w", pady=3)

        row_label(3, "保存到：")
        self.path_var = tk.StringVar(value=str(default_download_dir(settings)))
        ttk.Entry(frm, textvariable=self.path_var, width=48, font=T.FONT_UI).grid(
            row=3, column=1, sticky="ew", pady=3
        )
        ttk.Button(frm, text="浏览…", command=self._browse).grid(row=3, column=2, padx=(8, 0), pady=3)

        row_label(4, "连接数：")
        conn_fr = tk.Frame(frm, bg=T.BG)
        conn_fr.grid(row=4, column=1, sticky="w", pady=3)
        self.conn_var = tk.IntVar(value=int(settings.get("connections") or 8))
        ttk.Spinbox(conn_fr, from_=1, to=32, textvariable=self.conn_var, width=6).pack(side=tk.LEFT)
        tk.Label(
            conn_fr,
            text="  （分段下载）",
            bg=T.BG,
            fg=T.FG_MUTED,
            font=T.FONT_SMALL,
        ).pack(side=tk.LEFT)

        self.start_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm, text="立即开始下载", variable=self.start_var).grid(
            row=5, column=1, sticky="w", pady=8
        )

        btns = tk.Frame(frm, bg=T.BG)
        btns.grid(row=6, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="取消", command=self.destroy).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="开始下载", style="Primary.TButton", command=self._submit).pack(side=tk.RIGHT, padx=4)

        frm.columnconfigure(1, weight=1)
        self.url_var.trace_add("write", self._on_url_change)
        self.bind("<Return>", lambda e: self._submit())
        self.bind("<Escape>", lambda e: self.destroy())
        _fit_center(self, 600, 340)

    def _on_url_change(self, *_args: object) -> None:
        url = self.url_var.get().strip()
        if not self.name_var.get().strip() or self.name_var.get() in ("download", ""):
            self.name_var.set(suggest_filename(url))
        name = self.name_var.get().strip() or "download"
        cat = category_for_filename(name)
        self.cat_var.set(category_label(cat))
        folder = site_folder_for_url(self.settings, url) or self.settings.get("category_paths", {}).get(cat) or default_download_dir(self.settings)
        if folder:
            self.path_var.set(folder)

    def _browse(self) -> None:
        d = filedialog.askdirectory(initialdir=self.path_var.get() or None, parent=self, mustexist=False)
        if d:
            self.path_var.set(d)

    def _submit(self) -> None:
        url = self.url_var.get().strip()
        if not url or urlparse(url).scheme not in ("http", "https"):
            messagebox.showerror("链接无效", "请输入有效的 http(s) 链接。", parent=self)
            return
        media_kind = classify_url(url)
        media_type = media_kind.value
        name = self.name_var.get().strip() or suggest_filename(url)
        if media_type in ("hls", "dash"):
            # A streamed manifest becomes a single .mp4 after merging.
            stem = Path(name).stem or "video"
            if stem.lower().endswith((".m3u8", ".mpd", ".m3u")):
                stem = Path(stem).stem
            name = f"{stem}.mp4"
        for ch in '<>:"/\\|?*':
            name = name.replace(ch, "_")
        folder = migrated_path(
            self.path_var.get().strip() or str(resolve_save_path(self.settings, name).parent)
        )
        folder.mkdir(parents=True, exist_ok=True)
        #: if the name already exists, ask (overwrite / add version)
        # instead of silently appending "(1)".
        action, name = resolve_name_conflict(self, folder, name)
        if action == "cancel":
            return  # keep the dialog open so the user can change the name
        save_path = str(folder / name)
        if action == "overwrite":
            # Drop any stale partial so the download starts fresh; the finished
            # file replaces the existing one when it completes.
            Path(save_path + ".part").unlink(missing_ok=True)

        job = DownloadJob(
            url=url,
            save_path=save_path,
            filename=name,
            connections=max(1, min(32, int(self.conn_var.get()))),
            category="Video" if media_type in ("hls", "dash") else category_key(self.cat_var.get()),
            source="manual",
            media_type=media_type,
        )
        start = self.start_var.get()
        self.destroy()
        job._start_immediately = start  # type: ignore[attr-defined]
        self.on_submit(job)


class AddVideoDialog(tk.Toplevel):
    """Fetch a video's available qualities/formats and download the chosen one.

    ``probe_fn(url)`` runs the network probe (call returns the manager's
    probe_video result) — invoked on a worker thread.
    ``on_submit(url, folder, sel, media_type, title)`` starts the download.
    """

    def __init__(
        self,
        master: tk.Misc,
        settings: dict,
        on_submit: Callable[..., None],
        probe_fn: Callable[[str], dict],
        initial_url: str = "",
        submit_label: str = "下载",
        add_category: Callable[[str, str | None], str] | None = None,
    ) -> None:
        super().__init__(master)
        self.title("下载视频 — 选择画质")
        self.settings = settings
        self.on_submit = on_submit
        self.probe_fn = probe_fn
        self.add_category = add_category
        self._rows: dict[str, dict] = {}
        self._media_type = "page"
        self._title = ""
        self._probing = False
        self.resizable(True, True)
        self.transient(master)
        self.grab_set()
        self.configure(bg=T.BG)
        self.geometry("680x480")
        self.minsize(600, 420)

        header = tk.Frame(self, bg=T.BG_TOOLBAR, height=46)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(
            header, text="  🎬  下载视频", bg=T.BG_TOOLBAR,
            fg=T.FG_ON_DARK, font=T.FONT_TITLE, anchor="w",
        ).pack(fill=tk.BOTH, expand=True, padx=12)

        top = tk.Frame(self, bg=T.BG, padx=14, pady=10)
        top.pack(fill=tk.X)
        tk.Label(top, text="视频网页链接：", bg=T.BG, fg=T.FG, font=T.FONT_UI).grid(row=0, column=0, sticky="w")
        self.url_var = tk.StringVar(value=initial_url)
        ent = ttk.Entry(top, textvariable=self.url_var, font=T.FONT_UI)
        ent.grid(row=0, column=1, sticky="ew", padx=8)
        self.fetch_btn = ttk.Button(top, text="读取画质", command=self._fetch)
        self.fetch_btn.grid(row=0, column=2)
        top.columnconfigure(1, weight=1)
        ent.focus_set()
        ent.bind("<Return>", lambda e: self._fetch())

        self.status = tk.Label(self, text="输入视频链接后点击“读取画质”。", bg=T.BG, fg=T.FG_MUTED, font=T.FONT_SMALL, anchor="w")
        self.status.pack(fill=tk.X, padx=16)

        mid = tk.Frame(self, bg=T.BG, padx=14, pady=6)
        mid.pack(fill=tk.BOTH, expand=True)
        cols = ("quality", "format", "size", "note")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="browse", height=9)
        for key, label, w in (("quality", "画质", 150), ("format", "格式", 90), ("size", "大小", 100), ("note", "说明", 220)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=w, anchor="w")
        ysb = ttk.Scrollbar(mid, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ysb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Double-1>", lambda e: self._submit())

        bottom = tk.Frame(self, bg=T.BG, padx=14, pady=10)
        bottom.pack(fill=tk.X)
        bottom.columnconfigure(1, weight=1)

        tk.Label(bottom, text="分类：", bg=T.BG, fg=T.FG, font=T.FONT_UI).grid(row=0, column=0, sticky="w", pady=(0, 6))
        cat_row = tk.Frame(bottom, bg=T.BG)
        cat_row.grid(row=0, column=1, columnspan=2, sticky="ew", padx=8, pady=(0, 6))
        self.cats = list((settings.get("category_paths") or {}).keys()) or ["Video", "Music", "General"]
        self.cat_var = tk.StringVar(value=category_label("Video" if "Video" in self.cats else self.cats[0]))
        self.cat_cb = ttk.Combobox(cat_row, textvariable=self.cat_var, values=[category_label(c) for c in self.cats], state="readonly", width=18, font=T.FONT_UI)
        self.cat_cb.pack(side=tk.LEFT)
        self.cat_cb.bind("<<ComboboxSelected>>", lambda e: self._on_category())
        if self.add_category:
            ttk.Button(cat_row, text="＋ 新建…", command=self._new_category).pack(side=tk.LEFT, padx=(8, 0))

        tk.Label(bottom, text="保存到：", bg=T.BG, fg=T.FG, font=T.FONT_UI).grid(row=1, column=0, sticky="w")
        self.folder_var = tk.StringVar(
            value=site_folder_for_url(self.settings, initial_url)
            or (self.settings.get("category_paths", {}) or {}).get("Video")
            or str(default_download_dir(self.settings))
        )
        self.url_var.trace_add("write", self._on_url_change)
        ttk.Entry(bottom, textvariable=self.folder_var, font=T.FONT_UI).grid(row=1, column=1, sticky="ew", padx=8)
        ttk.Button(bottom, text="浏览…", command=self._browse).grid(row=1, column=2)

        btns = tk.Frame(self, bg=T.BG, padx=14, pady=8)
        btns.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(btns, text="取消", command=self.destroy).pack(side=tk.RIGHT, padx=4)
        self.dl_btn = ttk.Button(btns, text=f"  {submit_label}  ", command=self._submit)
        self.dl_btn.pack(side=tk.RIGHT, padx=4)

        _fit_center(self, 680, 480)
        if initial_url:
            self.after(150, self._fetch)

    def _browse(self) -> None:
        d = filedialog.askdirectory(initialdir=self.folder_var.get() or None, parent=self, mustexist=False)
        if d:
            self.folder_var.set(d)

    def _on_url_change(self, *_args: object) -> None:
        folder = site_folder_for_url(self.settings, self.url_var.get().strip())
        if folder:
            self.folder_var.set(str(folder))

    def _on_category(self) -> None:
        folder = (self.settings.get("category_paths") or {}).get(category_key(self.cat_var.get()))
        if folder:
            self.folder_var.set(folder)

    def _new_category(self) -> None:
        if not self.add_category:
            return
        name = simpledialog.askstring("新建分类", "分类名称：", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        folder = filedialog.askdirectory(
            title=f"“{name}”的文件夹（取消则使用默认位置）",
            initialdir=self.settings.get("default_save_path") or None,
            parent=self,
            mustexist=False,
        ) or None
        created = self.add_category(name, folder)
        if not created:
            return
        self.cats = list((self.settings.get("category_paths") or {}).keys())
        self.cat_cb["values"] = [category_label(c) for c in self.cats]
        self.cat_var.set(category_label(created))
        self._on_category()

    def _fetch(self) -> None:
        url = self.url_var.get().strip()
        if not url or urlparse(url).scheme not in ("http", "https"):
            messagebox.showerror("链接无效", "请输入有效的 http(s) 链接。", parent=self)
            return
        if self._probing:
            return
        self._probing = True
        self.fetch_btn.configure(state="disabled")
        self.status.configure(text="正在读取画质，请稍候…", fg=T.FG_MUTED)
        self.tree.delete(*self.tree.get_children())
        self._rows.clear()

        def worker() -> None:
            try:
                res = self.probe_fn(url)
                self.after(0, lambda: self._populate(res))
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda e=exc: self._probe_failed(str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _probe_failed(self, error: str) -> None:
        self._probing = False
        self.fetch_btn.configure(state="normal")
        self.status.configure(text=f"无法读取画质：{error[:120]}", fg=T.RED)

    def _populate(self, res: dict) -> None:
        self._probing = False
        self.fetch_btn.configure(state="normal")
        self._media_type = str(res.get("kind") or "page")
        self._title = str(res.get("title") or "")
        formats = res.get("formats") or []

        # Always offer a "best" auto option first.
        self._add_row("⭐ 推荐画质", "自动", "", "推荐", {})
        for fmt in formats:
            raw_size = _human_size(fmt.get("filesize") or 0)
            size = ("~" + raw_size) if (raw_size and fmt.get("approx")) else raw_size
            note = "需 ffmpeg 合并音视频" if fmt.get("needs_ffmpeg") else ("仅音频" if fmt.get("audio_only") else "")
            sel = {}
            if fmt.get("audio_only"):
                sel = {"audio_only": True}
            elif fmt.get("format_id"):
                sel = {"format_id": fmt["format_id"]}
            elif fmt.get("height"):
                sel = {"height": fmt["height"]}
            label = fmt.get("label") or (f"{fmt.get('height')}p" if fmt.get("height") else "格式")
            self._add_row(label, (fmt.get("ext") or "").upper(), size, note, sel)

        if not formats:
            self.status.configure(
                text=f"{self._title or '视频'} — 没有单独画质列表，可尝试推荐画质。",
                fg=T.ORANGE,
            )
        else:
            self.status.configure(text=f"{self._title or '视频'} — {len(formats)} 种画质，选择后点击下载。", fg=T.GREEN_DONE)
        first = self.tree.get_children()
        if first:
            self.tree.selection_set(first[0])

    def _add_row(self, quality: str, fmt: str, size: str, note: str, sel: dict) -> None:
        iid = self.tree.insert("", "end", values=(quality, fmt, size, note))
        self._rows[iid] = sel

    def _submit(self) -> None:
        url = self.url_var.get().strip()
        if not url or urlparse(url).scheme not in ("http", "https"):
            messagebox.showerror("链接无效", "请输入有效的 http(s) 链接。", parent=self)
            return
        folder = self.folder_var.get().strip()
        if not _confirm_create_folder(self, folder):
            return
        sel_id = self.tree.selection()
        sel = self._rows.get(sel_id[0], {}) if sel_id else {}
        category = category_key(self.cat_var.get())
        self.destroy()
        self.on_submit(url, folder, sel, self._media_type, self._title, category)


def _human_size(n: int) -> str:
    if not n:
        return ""
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024:
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TB"


class CaptureDialog(tk.Toplevel):
    """"Download File Info" dialog for browser-captured downloads.

    Prefilled from ``spec`` (manager.suggest_capture). Lets the user set the
    file name, category and save folder, then Start / queue (Later) / Cancel.
    ``on_result(final_spec, start, always_ask)`` is called on Start/Later.
    """

    def __init__(
        self,
        master: tk.Misc,
        settings: dict,
        spec: dict,
        on_result: Callable[[dict, bool, bool], None],
        add_category: Callable[[str, str | None], str] | None = None,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.title("下载 — 拾流下载器")
        self.settings = settings
        self.spec = dict(spec)
        self.on_result = on_result
        self.add_category = add_category
        self.on_closed = on_closed
        self._done = False
        self.transient(master)
        self.grab_set()
        self.configure(bg=T.BG)
        self.geometry("560x360")
        self.minsize(520, 340)

        header = tk.Frame(self, bg=T.BG_TOOLBAR, height=46)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        is_stream = spec.get("is_stream")
        htext = "  🎬  下载视频" if is_stream else "  ⬇  下载文件"
        tk.Label(header, text=htext, bg=T.BG_TOOLBAR, fg=T.FG_ON_DARK, font=T.FONT_TITLE, anchor="w").pack(
            fill=tk.BOTH, expand=True, padx=12
        )

        frm = tk.Frame(self, bg=T.BG, padx=16, pady=14)
        frm.pack(fill=tk.BOTH, expand=True)
        frm.columnconfigure(1, weight=1)

        def row(r: int, text: str) -> None:
            tk.Label(frm, text=text, bg=T.BG, fg=T.FG, font=T.FONT_UI, anchor="w").grid(
                row=r, column=0, sticky="w", pady=7, padx=(0, 10)
            )

        row(0, "文件名：")
        self.name_var = tk.StringVar(value=spec.get("filename", ""))
        ttk.Entry(frm, textvariable=self.name_var, font=T.FONT_UI).grid(row=0, column=1, columnspan=2, sticky="ew", pady=7)

        row(1, "分类：")
        cat_row = tk.Frame(frm, bg=T.BG)
        cat_row.grid(row=1, column=1, columnspan=2, sticky="ew", pady=7)
        self.cats = list((settings.get("category_paths") or {}).keys()) or ["General", "Video", "Music", "Documents", "Compressed"]
        self.cat_var = tk.StringVar(value=category_label(spec.get("category") or "General"))
        self.cat_cb = ttk.Combobox(cat_row, textvariable=self.cat_var, values=[category_label(c) for c in self.cats], state="readonly", width=20, font=T.FONT_UI)
        self.cat_cb.pack(side=tk.LEFT)
        self.cat_cb.bind("<<ComboboxSelected>>", lambda e: self._on_category())
        if self.add_category:
            ttk.Button(cat_row, text="＋ 新建分类…", command=self._new_category).pack(side=tk.LEFT, padx=(8, 0))

        row(2, "保存到：")
        # Default to the folder the user last downloaded to (remembered), else
        # the category folder from the spec.
        self.folder_var = tk.StringVar(value=spec.get("folder", "") or settings.get("last_save_dir", ""))
        ttk.Entry(frm, textvariable=self.folder_var, font=T.FONT_UI).grid(row=2, column=1, sticky="ew", pady=7)
        ttk.Button(frm, text="浏览…", command=self._browse).grid(row=2, column=2, padx=(8, 0), pady=7)

        row(3, "连接数：")
        self.conn_var = tk.IntVar(value=int(spec.get("connections") or 8))
        cframe = tk.Frame(frm, bg=T.BG)
        cframe.grid(row=3, column=1, sticky="w", pady=7)
        ttk.Spinbox(cframe, from_=1, to=32, textvariable=self.conn_var, width=6).pack(side=tk.LEFT)
        info = spec.get("media_type", "http")
        bits = []
        if is_stream:
            q = spec.get("media_meta", {}).get("quality") or (f"{spec['media_meta']['height']}p" if spec.get("media_meta", {}).get("height") else "推荐画质")
            bits.append(f"{info.upper()} 视频 · {q}")
        elif spec.get("size"):
            bits.append(_human_size(spec["size"]))
        if bits:
            tk.Label(cframe, text="   " + "  ·  ".join(bits), bg=T.BG, fg=T.FG_MUTED, font=T.FONT_SMALL).pack(side=tk.LEFT)

        tk.Label(frm, text="URL:", bg=T.BG, fg=T.FG_MUTED, font=T.FONT_SMALL, anchor="w").grid(row=4, column=0, sticky="w", pady=(10, 0))
        tk.Label(frm, text=(spec.get("url") or "")[:80] + ("…" if len(spec.get("url") or "") > 80 else ""),
                 bg=T.BG, fg=T.BLUE, font=T.FONT_SMALL, anchor="w").grid(row=4, column=1, columnspan=2, sticky="w", pady=(10, 0))

        self.always_var = tk.BooleanVar(value=bool(settings.get("confirm_browser_captures", True)))
        ttk.Checkbutton(frm, text="浏览器下载时始终显示此窗口", variable=self.always_var).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(14, 0)
        )

        btns = tk.Frame(self, bg=T.BG, padx=14, pady=10)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="取消", command=self._cancel).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="稍后下载", command=lambda: self._finish(False)).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="  开始下载  ", command=lambda: self._finish(True)).pack(side=tk.RIGHT, padx=4)

        self.bind("<Return>", lambda e: self._finish(True))
        self.bind("<Escape>", lambda e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        _fit_center(self, 560, 360)

    def _on_category(self) -> None:
        cat = category_key(self.cat_var.get())
        folder = (self.settings.get("category_paths") or {}).get(cat)
        if folder:
            self.folder_var.set(folder)

    def _new_category(self) -> None:
        if not self.add_category:
            return
        name = simpledialog.askstring("新建分类", "分类名称：", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        folder = filedialog.askdirectory(
            title=f"“{name}”的文件夹（取消则使用默认位置）",
            initialdir=self.settings.get("default_save_path") or None,
            parent=self,
            mustexist=False,
        ) or None
        created = self.add_category(name, folder)
        if not created:
            return
        self.cats = list((self.settings.get("category_paths") or {}).keys())
        self.cat_cb["values"] = [category_label(c) for c in self.cats]
        self.cat_var.set(category_label(created))
        self._on_category()

    def _browse(self) -> None:
        d = filedialog.askdirectory(initialdir=self.folder_var.get() or None, parent=self, mustexist=False)
        if d:
            self.folder_var.set(d)

    def _final(self) -> dict:
        s = dict(self.spec)
        s["filename"] = self.name_var.get().strip() or s.get("filename") or "download"
        s["category"] = category_key(self.cat_var.get())
        s["folder"] = self.folder_var.get().strip() or s.get("folder")
        s["connections"] = max(1, min(32, int(self.conn_var.get())))
        return s

    def _finish(self, start: bool) -> None:
        if self._done:
            return
        result = self._final()
        if not _confirm_create_folder(self, result["folder"]):
            return
        # filename collision prompt (overwrite / add version / cancel).
        action, newname = resolve_name_conflict(
            self, migrated_path(result["folder"]), result["filename"]
        )
        if action == "cancel":
            return  # keep the dialog open
        result["filename"] = newname
        result["overwrite"] = action == "overwrite"
        self._done = True
        always = bool(self.always_var.get())
        self.destroy()
        self.on_result(result, start, always)
        if self.on_closed:
            self.on_closed()

    def _cancel(self) -> None:
        if self._done:
            return
        self._done = True
        self.destroy()
        if self.on_closed:
            self.on_closed()


QUALITY_CHOICES = [
    ("每次询问", "ask"),
    ("最佳可用画质", "best"),
    ("2160p (4K)", "2160"),
    ("1440p", "1440"),
    ("1080p", "1080"),
    ("720p", "720"),
    ("480p", "480"),
    ("360p", "360"),
    ("仅音频", "audio"),
]


class SettingsDialog(tk.Toplevel):
    """Tabbed Options dialog."""

    def __init__(self, master: tk.Misc, settings: dict, on_save: Callable[[dict], None]) -> None:
        super().__init__(master)
        self.title("设置 — 拾流下载器")
        self.settings = dict(settings)
        self.on_save = on_save
        self.transient(master)
        self.grab_set()
        self.configure(bg=T.BG)
        self.geometry("640x510")
        self.minsize(620, 360)

        # Working copies of the editable category maps.
        self._cat_paths = dict(self.settings.get("category_paths") or {})
        self._cat_exts = {k: list(v) for k, v in (self.settings.get("category_extensions") or {}).items()}
        self._site_rules = [
            {"domain": str(r.get("domain") or "").strip(), "folder": str(r.get("folder") or "").strip()}
            for r in (self.settings.get("site_folder_rules") or []) if isinstance(r, dict)
        ]
        self._install_thread: threading.Thread | None = None

        header = tk.Frame(self, bg=T.BG_TOOLBAR, height=40)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(
            header, text="设置", bg=T.BG_TOOLBAR, fg=T.FG_ON_DARK,
            font=T.FONT_TITLE, anchor="w",
        ).pack(fill=tk.BOTH, expand=True, padx=12)

        # Reserve the button bar at the BOTTOM first so a tall notebook tab can
        # never clip it — the notebook then fills the space above it.
        btns = tk.Frame(self, bg=T.BG)
        btns.pack(fill=tk.X, side=tk.BOTTOM, padx=12, pady=10)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="保存", style="Primary.TButton", command=self._save).pack(side=tk.RIGHT, padx=4)

        nb = ttk.Notebook(self)
        nb.enable_traversal()
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 0))
        self._build_general(nb)
        self._build_connections(nb)
        self._build_filetypes(nb)
        self._build_video(nb)
        self._build_browser(nb)
        self._build_site_rules(nb)

        self._fit_tab(nb)
        nb.bind("<<NotebookTabChanged>>", lambda _e: self._fit_tab(nb))

    def _fit_tab(self, nb: ttk.Notebook) -> None:
        self.update_idletasks()
        page = self.nametowidget(nb.select())
        nb.configure(height=page.winfo_reqheight())
        _fit_center(self, 620, 360)

    # ── helpers ─────────────────────────────────────────────────────────
    def _tab(self, nb: ttk.Notebook, title: str) -> tk.Frame:
        f = tk.Frame(nb, bg=T.BG, padx=14, pady=9)
        nb.add(f, text=title)
        f.columnconfigure(1, weight=1)
        return f

    def _label(self, parent: tk.Frame, row: int, text: str) -> None:
        tk.Label(parent, text=text, bg=T.BG, fg=T.FG, font=T.FONT_UI, anchor="w").grid(
            row=row, column=0, sticky="w", pady=3, padx=(0, 10)
        )

    def _hint(self, parent: tk.Frame, row: int, text: str, col: int = 1, span: int = 2) -> None:
        tk.Label(
            parent, text=text, bg=T.BG, fg=T.FG_MUTED, font=T.FONT_SMALL,
            anchor="w", justify=tk.LEFT, wraplength=420,
        ).grid(row=row, column=col, columnspan=span, sticky="w", pady=(0, 6))

    # ── Tab: General ────────────────────────────────────────────────────
    def _build_general(self, nb: ttk.Notebook) -> None:
        f = self._tab(nb, "常规")
        self._label(f, 0, "默认保存文件夹：")
        self.path_var = tk.StringVar(value=str(default_download_dir(self.settings)))
        ttk.Entry(f, textvariable=self.path_var).grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Button(f, text="浏览…", command=lambda: self._browse_into(self.path_var)).grid(
            row=0, column=2, padx=(8, 0), pady=3
        )

        self.confirm_delete = tk.BooleanVar(value=bool(self.settings.get("confirm_delete", True)))
        ttk.Checkbutton(
            f, text="删除下载任务前询问", variable=self.confirm_delete
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=7)

        self.browser_auto = tk.BooleanVar(value=bool(self.settings.get("browser_auto_start", True)))
        ttk.Checkbutton(
            f, text="从浏览器添加后立即下载",
            variable=self.browser_auto,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=3)

        self.close_to_tray = tk.BooleanVar(value=bool(self.settings.get("close_to_tray", True)))
        ttk.Checkbutton(
            f, text="关闭窗口后继续在系统托盘运行（选择“退出”才会结束）",
            variable=self.close_to_tray,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=3)

        self.minimize_to_tray = tk.BooleanVar(value=bool(self.settings.get("minimize_to_tray", False)))
        ttk.Checkbutton(
            f, text="最小化时也隐藏到托盘", variable=self.minimize_to_tray
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=3)

        from magic_downloader import startup as _startup
        self.startup_var = tk.BooleanVar(value=_startup.is_enabled())
        startup_cb = ttk.Checkbutton(
            f, text="开机时启动 拾流下载器", variable=self.startup_var
        )
        startup_cb.grid(row=5, column=0, columnspan=3, sticky="w", pady=3)
        if not _startup.is_supported():
            startup_cb.configure(state="disabled")

    # ── Tab: Connections / speed ────────────────────────────────────────
    def _build_connections(self, nb: ttk.Notebook) -> None:
        f = self._tab(nb, "连接")
        self.conn_var = tk.IntVar(value=int(self.settings.get("connections") or 8))
        self.max_var = tk.IntVar(value=int(self.settings.get("max_simultaneous") or 3))
        self.workers_var = tk.IntVar(value=int(self.settings.get("media_workers") or 8))
        self.speed_var = tk.IntVar(value=int(self.settings.get("max_speed_kbps") or 0))
        self.timeout_var = tk.IntVar(value=int(self.settings.get("timeout") or 60))
        self.retries_var = tk.IntVar(value=int(self.settings.get("retries") or 3))
        self.chunk_var = tk.IntVar(value=int(int(self.settings.get("chunk_size") or 262144) // 1024))

        self._label(f, 0, "每个任务的连接数：")
        ttk.Spinbox(f, from_=1, to=32, textvariable=self.conn_var, width=8).grid(row=0, column=1, sticky="w", pady=3)
        self._label(f, 1, "同时下载任务数：")
        ttk.Spinbox(f, from_=1, to=10, textvariable=self.max_var, width=8).grid(row=1, column=1, sticky="w", pady=3)
        self._label(f, 2, "视频流分段线程数：")
        ttk.Spinbox(f, from_=1, to=32, textvariable=self.workers_var, width=8).grid(row=2, column=1, sticky="w", pady=3)

        self._label(f, 3, "速度限制（KB/s）：")
        ttk.Spinbox(f, from_=0, to=1000000, increment=64, textvariable=self.speed_var, width=10).grid(
            row=3, column=1, sticky="w", pady=3
        )
        self._hint(f, 4, "0 表示不限速，适用于所有进行中的任务。")

        self._label(f, 5, "请求超时（秒）：")
        ttk.Spinbox(f, from_=5, to=600, textvariable=self.timeout_var, width=8).grid(row=5, column=1, sticky="w", pady=3)
        self._label(f, 6, "出错重试次数：")
        ttk.Spinbox(f, from_=0, to=15, textvariable=self.retries_var, width=8).grid(row=6, column=1, sticky="w", pady=3)
        self._label(f, 7, "数据块大小（KB）：")
        ttk.Spinbox(f, from_=16, to=8192, increment=16, textvariable=self.chunk_var, width=8).grid(
            row=7, column=1, sticky="w", pady=3
        )

        self._label(f, 8, "User-Agent:")
        self.ua_var = tk.StringVar(value=str(self.settings.get("user_agent") or ""))
        ttk.Entry(f, textvariable=self.ua_var).grid(row=8, column=1, columnspan=2, sticky="ew", pady=3)

    # ── Tab: File Types (categories) ────────────────────────────────────
    def _build_filetypes(self, nb: ttk.Notebook) -> None:
        f = self._tab(nb, "文件类型")
        tk.Label(
            f, text="根据扩展名将下载文件归入以下分类。",
            bg=T.BG, fg=T.FG_MUTED, font=T.FONT_SMALL, anchor="w",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        left = tk.Frame(f, bg=T.BG)
        left.grid(row=1, column=0, sticky="ns")
        self.cat_listbox = tk.Listbox(
            left, height=8, width=14, exportselection=False,
            bg=T.BG_LIST, fg=T.FG, font=T.FONT_UI,
            selectbackground=T.SELECT, selectforeground=T.SELECT_FG, activestyle="none", relief="flat", borderwidth=0, highlightthickness=1, highlightbackground=T.BORDER,
        )
        self.cat_listbox.pack(fill=tk.Y, expand=True)
        self.cat_listbox.bind("<<ListboxSelect>>", lambda e: self._on_cat_select())
        catbtns = tk.Frame(f, bg=T.BG)
        catbtns.grid(row=2, column=0, sticky="w", pady=3)
        ttk.Button(catbtns, text="添加", width=7, command=self._add_category).pack(side=tk.LEFT)
        ttk.Button(catbtns, text="移除", width=8, command=self._remove_category).pack(side=tk.LEFT, padx=4)

        right = tk.Frame(f, bg=T.BG)
        right.grid(row=1, column=1, columnspan=2, sticky="nsew", padx=(14, 0))
        right.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        f.columnconfigure(1, weight=1)

        tk.Label(right, text="扩展名（用空格或逗号分隔）：", bg=T.BG, fg=T.FG, font=T.FONT_UI, anchor="w").pack(fill=tk.X)
        self.ext_var = tk.StringVar()
        self.ext_entry = tk.Text(right, height=5, width=40, font=T.FONT_UI, wrap="word", bg=T.BG, fg=T.FG, relief="flat", borderwidth=0, highlightthickness=1, highlightbackground=T.BORDER, highlightcolor=T.ACCENT)
        self.ext_entry.pack(fill=tk.BOTH, expand=True, pady=(2, 8))

        tk.Label(right, text="保存文件夹：", bg=T.BG, fg=T.FG, font=T.FONT_UI, anchor="w").pack(fill=tk.X)
        folder_row = tk.Frame(right, bg=T.BG)
        folder_row.pack(fill=tk.X, pady=(2, 8))
        self.cat_folder_var = tk.StringVar()
        ttk.Entry(folder_row, textvariable=self.cat_folder_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(folder_row, text="浏览…", command=lambda: self._browse_into(self.cat_folder_var)).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(right, text="应用到所选分类", command=self._apply_category).pack(anchor="e")

        self._reload_categories(select_first=True)

    def _all_categories(self) -> list[str]:
        names = ["General"] + [c for c in self._cat_paths if c != "General"]
        for c in self._cat_exts:
            if c not in names:
                names.append(c)
        return names

    def _reload_categories(self, select_first: bool = False) -> None:
        self.cat_listbox.delete(0, tk.END)
        for name in self._all_categories():
            self.cat_listbox.insert(tk.END, category_label(name))
        if select_first and self.cat_listbox.size():
            self.cat_listbox.selection_set(0)
            self._on_cat_select()

    def _selected_category(self) -> str | None:
        sel = self.cat_listbox.curselection()
        if not sel:
            return None
        return category_key(self.cat_listbox.get(sel[0]))

    def _on_cat_select(self) -> None:
        cat = self._selected_category()
        if not cat:
            return
        exts = self._cat_exts.get(cat, [])
        self.ext_entry.delete("1.0", tk.END)
        self.ext_entry.insert("1.0", " ".join(exts))
        self.cat_folder_var.set(self._cat_paths.get(cat, ""))

    def _apply_category(self) -> None:
        cat = self._selected_category()
        if not cat:
            return
        raw = self.ext_entry.get("1.0", tk.END).replace(",", " ").split()
        exts = []
        for tok in raw:
            tok = tok.strip().lower()
            if not tok:
                continue
            if not tok.startswith("."):
                tok = "." + tok
            exts.append(tok)
        if cat != "General":
            self._cat_exts[cat] = exts
        folder = self.cat_folder_var.get().strip()
        if folder:
            self._cat_paths[cat] = folder
        messagebox.showinfo("文件类型", f"已更新“{category_label(cat)}”。", parent=self)

    def _add_category(self) -> None:
        name = simpledialog.askstring("添加分类", "分类名称：", parent=self)
        if not name:
            return
        name = name.strip()
        if not name or name in self._all_categories():
            return
        base = default_download_dir(self.settings)
        self._cat_paths[name] = str(base / name)
        self._cat_exts[name] = []
        self._reload_categories()
        idx = self._all_categories().index(name)
        self.cat_listbox.selection_clear(0, tk.END)
        self.cat_listbox.selection_set(idx)
        self._on_cat_select()

    def _remove_category(self) -> None:
        cat = self._selected_category()
        if not cat or cat in ("General", "Video", "Music", "Documents", "Compressed"):
            messagebox.showinfo("文件类型", "内置分类不能移除。", parent=self)
            return
        self._cat_paths.pop(cat, None)
        self._cat_exts.pop(cat, None)
        self._reload_categories(select_first=True)

    # ── Tab: Video & ffmpeg ─────────────────────────────────────────────
    def _build_video(self, nb: ttk.Notebook) -> None:
        f = self._tab(nb, "视频 / ffmpeg")

        self._label(f, 0, "默认视频画质：")
        self.quality_var = tk.StringVar()
        cur_q = str(self.settings.get("default_video_quality") or "best")
        combo = ttk.Combobox(f, textvariable=self.quality_var, state="readonly", width=20,
                             values=[label for label, _ in QUALITY_CHOICES])
        combo.grid(row=0, column=1, sticky="w", pady=3)
        self.quality_var.set(next((lbl for lbl, val in QUALITY_CHOICES if val == cur_q), "最佳可用画质"))
        self._hint(f, 1, "用于一键下载。超过 720p 通常需要 ffmpeg 合并画面和声音。")

        sep = tk.Frame(f, bg=T.BORDER, height=1)
        sep.grid(row=2, column=0, columnspan=3, sticky="ew", pady=7)
        tk.Label(f, text="ffmpeg（将视频和音频合并为 MP4）", bg=T.BG, fg=T.ACCENT, font=T.FONT_MEDIUM, anchor="w").grid(
            row=3, column=0, columnspan=3, sticky="w"
        )

        self.ffmpeg_status = tk.Label(f, text="", bg=T.BG, fg=T.FG_MUTED, font=T.FONT_SMALL, anchor="w", justify=tk.LEFT, wraplength=440)
        self.ffmpeg_status.grid(row=4, column=0, columnspan=3, sticky="w", pady=3)

        self._label(f, 5, "ffmpeg 路径：")
        self.ffmpeg_var = tk.StringVar(value=str(self.settings.get("ffmpeg_path") or ""))
        ttk.Entry(f, textvariable=self.ffmpeg_var).grid(row=5, column=1, sticky="ew", pady=3)
        ttk.Button(f, text="浏览…", command=self._browse_ffmpeg).grid(row=5, column=2, padx=(8, 0), pady=3)

        self.install_btn = ttk.Button(f, text="自动安装 ffmpeg", command=self._install_ffmpeg)
        self.install_btn.grid(row=6, column=0, columnspan=2, sticky="w", pady=7)
        self.install_status = tk.Label(f, text="", bg=T.BG, fg=T.FG_MUTED, font=T.FONT_SMALL, anchor="w")
        self.install_status.grid(row=7, column=0, columnspan=3, sticky="w")

        sep2 = tk.Frame(f, bg=T.BORDER, height=1)
        sep2.grid(row=8, column=0, columnspan=3, sticky="ew", pady=7)
        self.ts_out_var = tk.BooleanVar(value=bool(self.settings.get("stream_output_ts", False)))
        ttk.Checkbutton(f, text="将视频流保存为原始 .ts（不合并成 .mp4）",
                        variable=self.ts_out_var).grid(row=9, column=0, columnspan=3, sticky="w")
        self._hint(f, 10, "关闭时生成可拖动进度的 .mp4（推荐）。开启时保存为 MPEG-TS，适用于 HLS/DASH 视频。")

        self.smaller_var = tk.BooleanVar(value=bool(self.settings.get("prefer_smaller_files", False)))
        ttk.Checkbutton(f, text="优先较小文件（AV1/VP9 编码或较低码率）",
                        variable=self.smaller_var).grid(row=11, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self._hint(f, 12, "一键下载时，在相同分辨率中优先选择体积更小的编码。")
        self._refresh_ffmpeg_status()

    def _refresh_ffmpeg_status(self) -> None:
        ffmpeg_mod.reset_cache()
        hint = self.ffmpeg_var.get().strip() or None
        found = ffmpeg_mod.find_ffmpeg(extra_hint=hint)
        if found:
            self.ffmpeg_status.configure(text=f"已找到：{found}", fg=T.GREEN_DONE)
        else:
            self.ffmpeg_status.configure(
                text="未找到。安装 ffmpeg 前，视频流可能保存为 .ts 或分离文件。",
                fg=T.RED,
            )

    def _browse_ffmpeg(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 ffmpeg 程序",
            filetypes=[("ffmpeg", "ffmpeg.exe ffmpeg"), ("所有文件", "*.*")],
        )
        if path:
            self.ffmpeg_var.set(path)
            self._refresh_ffmpeg_status()

    def _install_ffmpeg(self) -> None:
        if self._install_thread and self._install_thread.is_alive():
            return
        self.install_btn.configure(state="disabled")
        self.install_status.configure(text="开始下载…", fg=T.FG_MUTED)

        def worker() -> None:
            from magic_downloader.media import ffmpeg_installer

            def prog(done: int, total: int, phase: str) -> None:
                if total:
                    pct = int(done * 100 / total)
                    msg = f"{phase} {pct}%  ({done // (1024*1024)} MB)"
                else:
                    msg = phase
                self.after(0, lambda: self.install_status.configure(text=msg))

            try:
                path = ffmpeg_installer.install_ffmpeg(progress=prog)
                self.after(0, lambda: self._install_done(path, None))
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda e=exc: self._install_done(None, str(e)))

        self._install_thread = threading.Thread(target=worker, daemon=True)
        self._install_thread.start()

    def _install_done(self, path: str | None, error: str | None) -> None:
        self.install_btn.configure(state="normal")
        if path:
            self.install_status.configure(text=f"✅ 已安装：{path}", fg=T.GREEN_DONE)
            self._refresh_ffmpeg_status()
        else:
            self.install_status.configure(text=f"失败：{error}", fg=T.RED)
            messagebox.showerror(
                "安装 ffmpeg",
                f"无法自动下载 ffmpeg：\n{error}\n\n"
                "可手动安装，并在上方设置程序路径。",
                parent=self,
            )

    # ── Tab: Browser ────────────────────────────────────────────────────
    def _build_browser(self, nb: ttk.Notebook) -> None:
        f = self._tab(nb, "浏览器")
        self.browser_on = tk.BooleanVar(value=bool(self.settings.get("browser_integration", True)))
        ttk.Checkbutton(
            f, text="启用本地浏览器连接（扩展必需）", variable=self.browser_on
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=3)

        self.confirm_captures = tk.BooleanVar(value=bool(self.settings.get("confirm_browser_captures", True)))
        ttk.Checkbutton(
            f, text="浏览器下载时显示文件名、分类和文件夹设置窗口",
            variable=self.confirm_captures,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=3)

        self._label(f, 2, "连接端口：")
        self.port_var = tk.IntVar(value=int(self.settings.get("browser_port") or 7374))
        ttk.Spinbox(f, from_=1024, to=65535, textvariable=self.port_var, width=10).grid(row=2, column=1, sticky="w", pady=3)

        self._label(f, 3, "连接口令（可选）：")
        self.token_var = tk.StringVar(value=str(self.settings.get("browser_token") or ""))
        ttk.Entry(f, textvariable=self.token_var).grid(row=3, column=1, columnspan=2, sticky="ew", pady=3)

        sep = tk.Frame(f, bg=T.BORDER, height=1)
        sep.grid(row=4, column=0, columnspan=3, sticky="ew", pady=7)
        tk.Label(f, text="浏览器扩展", bg=T.BG, fg=T.ACCENT,
                 font=T.FONT_MEDIUM, anchor="w").grid(row=5, column=0, columnspan=3, sticky="w")

        self._ext_rows = tk.Frame(f, bg=T.BG)
        self._ext_rows.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(6, 0))

        self.ext_status = tk.Label(f, text="", bg=T.BG, fg=T.FG_MUTED, font=T.FONT_SMALL,
                                   anchor="w", justify=tk.LEFT, wraplength=440)
        self.ext_status.grid(row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self._build_extension_rows()

        self._hint(
            f, 8,
            "请在浏览器的扩展管理页手动加载随附的 browser_extension 文件夹。",
            col=0, span=3,
        )

    # ── browser extension install ───────────────────────────────────────
    def _build_extension_rows(self) -> None:
        for w in self._ext_rows.winfo_children():
            w.destroy()
        from magic_downloader.paths import extension_dir

        tk.Label(
            self._ext_rows,
            text=f"定制版扩展文件夹：\n{extension_dir()}",
            bg=T.BG, fg=T.FG, font=T.FONT_SMALL, anchor="w", justify=tk.LEFT,
            wraplength=440,
        ).pack(fill=tk.X)

    # ── Tab: Site folders ───────────────────────────────────────────────
    def _build_site_rules(self, nb: ttk.Notebook) -> None:
        f = self._tab(nb, "站点归档")
        tk.Label(
            f, text="域名命中时优先保存到指定文件夹。支持子域名，例如 douyin.com 会匹配 www.douyin.com。",
            bg=T.BG, fg=T.FG_MUTED, font=T.FONT_SMALL, anchor="w", justify=tk.LEFT, wraplength=520,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.site_rule_list = tk.Listbox(
            f, height=6, bg=T.BG_LIST, fg=T.FG, font=T.FONT_UI, exportselection=False,
            selectbackground=T.SELECT, selectforeground=T.SELECT_FG, activestyle="none", relief="flat", borderwidth=0, highlightthickness=1, highlightbackground=T.BORDER,
        )
        self.site_rule_list.grid(row=1, column=0, columnspan=3, sticky="nsew")
        self.site_rule_list.bind("<<ListboxSelect>>", lambda _e: self._on_site_rule_select())
        f.rowconfigure(1, weight=1)
        self._label(f, 2, "网站域名：")
        self.site_domain_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.site_domain_var).grid(row=2, column=1, columnspan=2, sticky="ew", pady=3)
        self._label(f, 3, "保存文件夹：")
        self.site_folder_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.site_folder_var).grid(row=3, column=1, sticky="ew", pady=3)
        ttk.Button(f, text="浏览…", command=lambda: self._browse_into(self.site_folder_var)).grid(row=3, column=2, padx=(8, 0), pady=3)
        buttons = tk.Frame(f, bg=T.BG)
        buttons.grid(row=4, column=1, columnspan=2, sticky="e", pady=7)
        ttk.Button(buttons, text="新增 / 更新", command=self._save_site_rule).pack(side=tk.LEFT)
        ttk.Button(buttons, text="移除", command=self._remove_site_rule).pack(side=tk.LEFT, padx=(6, 0))
        self._reload_site_rules()

    def _reload_site_rules(self) -> None:
        self.site_rule_list.delete(0, tk.END)
        for rule in self._site_rules:
            self.site_rule_list.insert(tk.END, f"{rule['domain']}  →  {rule['folder']}")

    def _on_site_rule_select(self) -> None:
        selected = self.site_rule_list.curselection()
        if selected:
            rule = self._site_rules[selected[0]]
            self.site_domain_var.set(rule["domain"])
            self.site_folder_var.set(rule["folder"])

    def _save_site_rule(self) -> None:
        from urllib.parse import urlsplit
        raw = self.site_domain_var.get().strip().lower().lstrip("*.")
        domain = (urlsplit(raw if "://" in raw else "https://" + raw).hostname or "").strip(".")
        folder = self.site_folder_var.get().strip()
        if not domain or "." not in domain or not folder:
            messagebox.showerror("站点归档", "请输入网站域名和保存文件夹。", parent=self)
            return
        selected = self.site_rule_list.curselection()
        rule = {"domain": domain, "folder": folder}
        if selected:
            self._site_rules[selected[0]] = rule
        else:
            self._site_rules.append(rule)
        self._reload_site_rules()

    def _remove_site_rule(self) -> None:
        selected = self.site_rule_list.curselection()
        if selected:
            self._site_rules.pop(selected[0])
            self.site_domain_var.set("")
            self.site_folder_var.set("")
            self._reload_site_rules()

    # ── save ────────────────────────────────────────────────────────────
    def _browse_into(self, var: tk.StringVar) -> None:
        d = filedialog.askdirectory(initialdir=var.get() or None, parent=self, mustexist=False)
        if d:
            var.set(d)

    def _save(self) -> None:
        old_default = Path(self.settings.get("default_save_path") or "")
        self.settings["default_save_path"] = self.path_var.get().strip()
        self.settings["confirm_delete"] = bool(self.confirm_delete.get())
        self.settings["browser_auto_start"] = bool(self.browser_auto.get())
        self.settings["close_to_tray"] = bool(self.close_to_tray.get())
        self.settings["minimize_to_tray"] = bool(self.minimize_to_tray.get())
        try:
            from magic_downloader import startup as _startup

            _startup.set_enabled(bool(self.startup_var.get()))
        except Exception:
            pass

        self.settings["connections"] = max(1, min(32, int(self.conn_var.get())))
        self.settings["max_simultaneous"] = max(1, min(10, int(self.max_var.get())))
        self.settings["media_workers"] = max(1, min(32, int(self.workers_var.get())))
        self.settings["max_speed_kbps"] = max(0, int(self.speed_var.get()))
        self.settings["timeout"] = max(5, min(600, int(self.timeout_var.get())))
        self.settings["retries"] = max(0, min(15, int(self.retries_var.get())))
        self.settings["chunk_size"] = max(16, min(8192, int(self.chunk_var.get()))) * 1024
        if self.ua_var.get().strip():
            self.settings["user_agent"] = self.ua_var.get().strip()

        # File types (fold in any unsaved edits to the selected category first).
        self._apply_current_category_silent()
        if self.settings["default_save_path"] and old_default != Path(self.settings["default_save_path"]):
            for category, folder in self._cat_paths.items():
                if Path(folder) in (old_default, old_default / category):
                    self._cat_paths[category] = self.settings["default_save_path"]
        self.settings["category_paths"] = dict(self._cat_paths)
        self.settings["category_extensions"] = {k: list(v) for k, v in self._cat_exts.items()}
        self.settings["site_folder_rules"] = [r for r in self._site_rules if r["domain"] and r["folder"]]

        self.settings["default_video_quality"] = next(
            (val for lbl, val in QUALITY_CHOICES if lbl == self.quality_var.get()), "best"
        )
        self.settings["ffmpeg_path"] = self.ffmpeg_var.get().strip()
        self.settings["stream_output_ts"] = bool(self.ts_out_var.get())
        self.settings["prefer_smaller_files"] = bool(self.smaller_var.get())

        self.settings["browser_integration"] = bool(self.browser_on.get())
        self.settings["confirm_browser_captures"] = bool(self.confirm_captures.get())
        self.settings["browser_port"] = max(1024, min(65535, int(self.port_var.get())))
        self.settings["browser_token"] = self.token_var.get().strip()

        self.on_save(self.settings)
        self.destroy()

    def _apply_current_category_silent(self) -> None:
        cat = self._selected_category()
        if not cat:
            return
        raw = self.ext_entry.get("1.0", tk.END).replace(",", " ").split()
        exts = []
        for tok in raw:
            tok = tok.strip().lower()
            if tok:
                exts.append(tok if tok.startswith(".") else "." + tok)
        if cat != "General":
            self._cat_exts[cat] = exts
        folder = self.cat_folder_var.get().strip()
        if folder:
            self._cat_paths[cat] = folder


class DownloadProgressDialog(tk.Toplevel):
    """per-download progress window (modeless — several can be open).

    Reads live state from the manager; the app drives ``update_view`` each tick.
    ``open_path(Path)`` opens a file/folder.
    """

    def __init__(self, master: tk.Misc, manager, job_id: str, open_path: Callable[[Path], None],
                 on_fold: Callable[[str], None] | None = None) -> None:
        super().__init__(master)
        self.manager = manager
        self.job_id = job_id
        self.open_path = open_path
        self.on_fold = on_fold      # app hook: register this download in the tray
        self._closed = False
        # "Close when complete" should fire only when the download FINISHES while
        # this window is open — not when the window is opened for an already-done
        # download (which would just flash and vanish). Track whether we ever saw
        # it mid-download, and schedule the close at most once.
        self._saw_incomplete = False
        self._close_scheduled = False
        self.title("下载进度 — 拾流下载器")
        self.configure(bg=T.BG)
        self.geometry("580x350")
        self.minsize(540, 330)
        # NOT transient: a transient Toplevel gets no taskbar button and no
        # minimize box on Windows. Keeping it independent lets each download
        # window fold down to the taskbar and be restored from its icon.

        job = self.manager.get_job(job_id)
        header = tk.Frame(self, bg=T.BG_TOOLBAR, height=44)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        # Fold-to-tray lives on the title bar (IDM-style), packed first so the
        # title fills the space to its left. Shown only while the download is
        # still running (update_view hides it once it's finished).
        if self.on_fold is not None:
            self.tray_btn = tk.Button(
                header, text=" ▾ 托盘 ", command=self._fold_to_tray,
                bg=T.BG_TOOLBAR, fg=T.FG_ON_DARK, relief="flat", bd=0,
                activebackground=T.ACCENT_HOVER, activeforeground="white",
                font=T.FONT_TOOLBAR, cursor="hand2",
            )
            self.tray_btn.pack(side=tk.RIGHT, padx=(0, 8))
        else:
            self.tray_btn = None
        self.title_lbl = tk.Label(
            header, text=f"  ⬇  {job.filename if job else '下载'}", bg=T.BG_TOOLBAR,
            fg=T.FG_ON_DARK, font=T.FONT_TITLE, anchor="w",
        )
        self.title_lbl.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=12)

        body = tk.Frame(self, bg=T.BG, padx=16, pady=12)
        body.pack(fill=tk.BOTH, expand=True)
        self.url_lbl = tk.Label(body, text="", bg=T.BG, fg=T.BLUE, font=T.FONT_SMALL, anchor="w")
        self.url_lbl.pack(fill=tk.X)
        self.path_lbl = tk.Label(body, text="", bg=T.BG, fg=T.FG_MUTED, font=T.FONT_SMALL, anchor="w")
        self.path_lbl.pack(fill=tk.X, pady=(0, 8))

        self.bar = ProgressBar(body, height=22)
        self.bar.pack(fill=tk.X)

        stats = tk.Frame(body, bg=T.BG)
        stats.pack(fill=tk.X, pady=8)
        self._stat: dict[str, tk.Label] = {}
        for col, (key, title) in enumerate(
            [("status", "状态"), ("size", "已下载"), ("speed", "速度"),
             ("eta", "剩余时间"), ("parts", "连接数")]
        ):
            f = tk.Frame(stats, bg=T.BG)
            f.grid(row=0, column=col, sticky="w", padx=(0, 16))
            tk.Label(f, text=title, bg=T.BG, fg=T.FG_MUTED, font=T.FONT_AUX).pack(anchor="w")
            v = tk.Label(f, text="—", bg=T.BG, fg=T.FG, font=T.FONT_MEDIUM)
            v.pack(anchor="w")
            self._stat[key] = v

        tk.Label(body, text="下载进度（连接）：", bg=T.BG, fg=T.FG_MUTED,
                 font=T.FONT_SMALL, anchor="w").pack(fill=tk.X)
        self.segbar = SegmentBar(body, height=34)
        self.segbar.pack(fill=tk.X, pady=(2, 8))

        self.close_when_done = tk.BooleanVar(
            value=bool(self.manager.settings.get("progress_close_on_complete", False))
        )
        ttk.Checkbutton(
            body, text="下载完成后关闭此窗口",
            variable=self.close_when_done, command=self._remember_close_pref,
        ).pack(anchor="w")

        btns = tk.Frame(self, bg=T.BG, padx=14, pady=10)
        btns.pack(fill=tk.X)
        self.pause_btn = ttk.Button(btns, text="暂停", command=self._toggle)
        self.pause_btn.pack(side=tk.LEFT)
        self.cancel_btn = ttk.Button(btns, text="取消", command=self._cancel)
        self.cancel_btn.pack(side=tk.LEFT, padx=6)
        self.open_btn = ttk.Button(btns, text="打开文件", command=self._open_file)  # shown when complete
        # (Fold-to-tray moved to the title bar; see the header above.)
        ttk.Button(btns, text="隐藏", command=self._hide).pack(side=tk.RIGHT, padx=4)
        self.folder_btn = ttk.Button(btns, text="打开文件夹", command=self._open_folder)
        self.folder_btn.pack(side=tk.RIGHT, padx=4)

        self.protocol("WM_DELETE_WINDOW", self._hide)
        _fit_center(self, 580, 350)
        self.update_view()

    def _remember_close_pref(self) -> None:
        self.manager.settings["progress_close_on_complete"] = bool(self.close_when_done.get())
        try:
            self.manager.save_settings()
        except Exception:
            pass

    def update_view(self) -> None:
        if self._closed:
            return
        job = self.manager.get_job(self.job_id)
        if job is None:
            self._closed = True
            self.destroy()
            return

        if job.status != DownloadStatus.COMPLETE:
            self._saw_incomplete = True     # it was still running at some point

        # Fold-to-tray only makes sense while the download is still running;
        # hide the title-bar button once it's finished/failed/cancelled.
        if self.tray_btn is not None:
            terminal = job.status in (
                DownloadStatus.COMPLETE, DownloadStatus.FAILED, DownloadStatus.CANCELLED)
            if terminal and self.tray_btn.winfo_ismapped():
                self.tray_btn.pack_forget()
            elif not terminal and not self.tray_btn.winfo_ismapped():
                self.tray_btn.pack(side=tk.RIGHT, padx=(0, 8))

        active = job.status == DownloadStatus.DOWNLOADING
        processing = job.status == DownloadStatus.PROCESSING
        icon = "✅" if job.status == DownloadStatus.COMPLETE else ("⚠" if job.status == DownloadStatus.FAILED else "⬇")
        self.title_lbl.configure(text=f"  {icon}  {job.filename}")
        self.url_lbl.configure(text=(job.url or "")[:95])
        self.path_lbl.configure(text=f"保存到：{job.save_path}")
        self.bar.set_progress(job.progress, active=active or processing)

        status = job.status.value
        if processing:
            status = "正在合并…"
        elif job.status == DownloadStatus.FAILED and job.error:
            status = f"失败：{job.error[:40]}"
        else:
            status = {
                "queued": "排队中", "connecting": "连接中", "downloading": "下载中",
                "paused": "已暂停", "processing": "处理中", "complete": "已完成",
                "failed": "失败", "cancelled": "已取消",
            }.get(status.lower(), status)
        self._stat["status"].configure(text=status)

        if job.is_stream and job.media_meta.get("seg_total"):
            self._stat["size"].configure(
                text=f"{job.media_meta.get('seg_done', 0)} / {job.media_meta['seg_total']} 段"
            )
        else:
            size = format_bytes(job.downloaded) + (f" / {format_bytes(job.total_size)}" if job.total_size else "")
            self._stat["size"].configure(text=size or "—")
        self._stat["speed"].configure(text=format_speed(job.speed_bps) if active else "—")
        self._stat["eta"].configure(text=format_eta(job.eta_seconds) if active else "—")
        self._stat["parts"].configure(
            text=job.media_type.upper() if job.is_stream else (str(job.connections) if job.supports_ranges else "1")
        )
        self.segbar.set_job(job)

        busy = job.status in (
            DownloadStatus.DOWNLOADING, DownloadStatus.CONNECTING, DownloadStatus.QUEUED, DownloadStatus.PROCESSING,
        )
        if busy:
            self.pause_btn.configure(text="暂停", state="normal")
            self.cancel_btn.configure(state="normal")
            self.open_btn.pack_forget()
        elif job.status == DownloadStatus.PAUSED:
            self.pause_btn.configure(text="继续", state="normal")
            self.cancel_btn.configure(state="normal")
            self.open_btn.pack_forget()
        elif job.status == DownloadStatus.COMPLETE:
            self.pause_btn.configure(state="disabled")
            self.cancel_btn.configure(state="disabled")
            self.open_btn.pack(side=tk.LEFT, padx=6)
            # Only when it finished while open (not opened already-complete), and
            # only schedule the close once.
            if (self.close_when_done.get() and self._saw_incomplete
                    and not self._close_scheduled):
                self._close_scheduled = True
                self.after(1200, self._hide)
        else:  # FAILED / CANCELLED
            self.pause_btn.configure(text="重试", state="normal")
            self.cancel_btn.configure(state="disabled")
            self.open_btn.pack_forget()

    def _toggle(self) -> None:
        job = self.manager.get_job(self.job_id)
        if not job:
            return
        if job.status in (DownloadStatus.DOWNLOADING, DownloadStatus.CONNECTING):
            self.manager.pause_job(self.job_id)
        else:
            self.manager.retry_job(self.job_id)
        self.update_view()

    def _cancel(self) -> None:
        self.manager.cancel_job(self.job_id)
        self.update_view()

    def _open_file(self) -> None:
        job = self.manager.get_job(self.job_id)
        if job:
            self.open_path(migrated_path(job.save_path))

    def _open_folder(self) -> None:
        job = self.manager.get_job(self.job_id)
        if job:
            self.open_path(migrated_path(job.save_path).parent)

    def _hide(self) -> None:
        # Just close the window — does NOT cancel the download.
        self._closed = True
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _fold_to_tray(self) -> None:
        """Hide the window but keep it alive; the app lists it in the tray menu."""
        try:
            self.withdraw()
        except tk.TclError:
            return
        if self.on_fold is not None:
            self.on_fold(self.job_id)

    def restore(self) -> None:
        """Bring a folded (or minimized) window back to the front."""
        try:
            self.deiconify()
            self.state("normal")
            self.lift()
            self.focus_force()
        except tk.TclError:
            pass


class AboutDialog(tk.Toplevel):
    """拾流下载器 product information, with no network update actions."""

    def __init__(self, master: tk.Misc, version: str, logo=None) -> None:
        super().__init__(master)
        self.title("关于 拾流下载器")
        self.configure(bg=T.BG)
        self.resizable(False, False)
        self.transient(master)

        head = tk.Frame(self, bg=T.BG_TOOLBAR)
        head.pack(fill=tk.X)
        inner = tk.Frame(head, bg=T.BG_TOOLBAR)
        inner.pack(padx=16, pady=14)
        if logo is not None:
            tk.Label(inner, image=logo, bg=T.BG_TOOLBAR).pack(side=tk.LEFT, padx=(0, 12))
        txt = tk.Frame(inner, bg=T.BG_TOOLBAR)
        txt.pack(side=tk.LEFT)
        tk.Label(txt, text="拾流下载器", bg=T.BG_TOOLBAR, fg=T.FG_ON_DARK,
                 font=T.FONT_TITLE).pack(anchor="w")
        tk.Label(txt, text=f"版本 {version}", bg=T.BG_TOOLBAR,
                 fg=T.FG_ON_DARK_MUTED, font=T.FONT_UI).pack(anchor="w")

        body = tk.Frame(self, bg=T.BG, padx=18, pady=16)
        body.pack(fill=tk.BOTH, expand=True)
        tk.Label(body, text="多线程下载 · 断点续传 · 浏览器视频识别",
                 bg=T.BG, fg=T.FG, font=T.FONT_UI, anchor="w").pack(fill=tk.X)
        tk.Label(body, text="使用时请遵守内容授权与网站规则。",
                 bg=T.BG, fg=T.FG_MUTED, font=T.FONT_SMALL,
                 anchor="w").pack(fill=tk.X, pady=(8, 0))
        ttk.Button(body, text="关闭", command=self.destroy).pack(anchor="e", pady=(18, 0))
        _center(self)
        self.grab_set()
