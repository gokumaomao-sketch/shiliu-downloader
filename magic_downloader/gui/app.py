"""Main window — classic download-manager layout."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from magic_downloader.paths import migrated_path
from tkinter import filedialog, ttk

from magic_downloader.gui import quiet_dialogs as messagebox
from urllib.parse import urlparse

from magic_downloader.config import default_download_dir
from magic_downloader.browser_server import BrowserAPIServer
if sys.platform == "win32":
    from magic_downloader.channels_service import ChannelsService
elif sys.platform == "darwin":
    from magic_downloader.channels_macos import MacChannelsService
from magic_downloader.gui import theme as T
from magic_downloader.gui.dialogs import (
    AboutDialog,
    AddDownloadDialog,
    AddVideoDialog,
    CaptureDialog,
    DownloadProgressDialog,
    SettingsDialog,
)
from magic_downloader.gui.widgets import ProgressBar, SegmentBar, ToolbarButton, Sidebar, TaskRows
from magic_downloader.manager import DownloadManager
from magic_downloader.models import (
    DownloadJob,
    DownloadStatus,
    format_bytes,
    format_eta,
    format_speed,
)

COLUMNS = ("filename", "folder", "size", "status", "progress", "speed", "avg",
           "elapsed", "eta", "date", "conn", "category")
# The list would be unusable with nothing in it, so this one always stays.
ALWAYS_SHOWN = "filename"

# Sidebar filter keys
FILTER_ALL = "all"
FILTER_DOWNLOADING = "downloading"
FILTER_QUEUED = "queued"
FILTER_PAUSED = "paused"
FILTER_COMPLETE = "complete"
FILTER_FAILED = "failed"
FILTER_CAT_PREFIX = "cat:"

STATUS_CN = {
    "Queued": "排队中", "Connecting": "连接中", "Downloading": "下载中",
    "Processing": "合并中", "Paused": "已暂停", "Complete": "已完成",
    "Failed": "失败", "Cancelled": "已取消",
}
CATEGORY_CN = {
    "General": "常规", "Compressed": "压缩文件", "Documents": "文档",
    "Music": "音乐", "Video": "视频",
}


class MagicDownloaderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        # Silence Tk's bell so no widget ever beeps (backspace in an empty
        # field, a Spinbox hitting its limit, etc.). Replace the built-in
        # `bell` command with a no-op — purely removes the sound.
        try:
            self.tk.eval("catch {rename bell {}}; proc bell args {}")
        except tk.TclError:
            pass
        # Log any exception raised inside a Tk callback (invisible otherwise in
        # the console-less frozen app) so silent failures can be diagnosed.
        T.apply_fonts(self)
        self.report_callback_exception = self._log_tk_exception
        from magic_downloader import __version__
        self.title("拾流下载器")
        self.geometry("1360x820")
        self.minsize(1080, 640)
        self.configure(bg=T.BG)
        self._set_window_icon()

        self.manager = DownloadManager()
        self.manager.add_listener(self._schedule_refresh)
        if sys.platform == "darwin":
            self._log_runtime_paths()
        self._channels = ChannelsService() if sys.platform == "win32" else MacChannelsService() if sys.platform == "darwin" else None
        self._filter = FILTER_ALL
        # None = the order downloads were added, which is what the list showed
        # before any header is clicked.
        self._sort_col: str | None = None
        self._sort_reverse = False
        self._browser: BrowserAPIServer | None = None
        self._toast_after: str | None = None
        self._tray = None
        self._tray_thread: object | None = None
        self._quitting = False
        self._single_instance = None
        self._capture_queue: list[dict] = []
        self._capture_active = False
        self._progress_dialogs: dict[str, DownloadProgressDialog] = {}
        self._folded_downloads: list[str] = []   # progress dialogs folded to tray
        self._folded_snapshot: list = []          # lock-free (id, label) for the tray menu

        self._record_version()         # stamp "updated at" on a new version
        self._build_menu()
        self._build_statusbar()   # reserve the bottom bar BEFORE the expanding body
        self._build_body()
        self._apply_style()
        self._start_browser_server()
        self._setup_tray()

        self.bind("<Control-n>", lambda e: self._add_url())
        self.bind("<Control-N>", lambda e: self._add_url())
        self.bind("<Control-d>", lambda e: self._add_video())
        self.bind("<Control-D>", lambda e: self._add_video())
        self.bind("<Control-v>", lambda e: self._paste_url())
        self.bind("<Control-V>", lambda e: self._paste_url())
        self.bind("<Delete>", lambda e: self._delete_selected())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Unmap>", self._on_minimize)

        self._refresh_all()
        self.after(400, self._tick)

    # ── chrome ──────────────────────────────────────────────────────────

    def _apply_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Treeview",
            rowheight=37,
            font=T.FONT_TASK,
            background=T.BG_LIST,
            fieldbackground=T.BG_LIST,
            foreground=T.FG,
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            font=T.FONT_HEADER,
            background=T.BG_SIDEBAR,
            foreground=T.FG_MUTED,
            relief="flat",
            borderwidth=0,
            padding=(4, 4),
        )
        # clam draws a bevelled box around every heading cell; borderwidth=0
        # doesn't remove it, the layout has to. This keeps the text element
        # (which carries the ▲/▼ sort arrow) and drops the frame.
        try:
            style.layout("Treeview.Heading", [
                ("Treeheading.cell", {"sticky": "nswe"}),
                ("Treeheading.padding", {"sticky": "nswe", "children": [
                    ("Treeheading.text", {"sticky": "w"})]}),
            ])
        except tk.TclError:
            pass
        style.map("Treeview.Heading", background=[("active", T.BG)])
        style.map(
            "Treeview",
            background=[("selected", T.SELECT)],
            foreground=[("selected", T.SELECT_FG)],
        )
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        for orientation, sticky in (("Vertical", "ns"), ("Horizontal", "we")):
            style.layout(f"{orientation}.TScrollbar", [
                (f"{orientation}.Scrollbar.trough", {"sticky": sticky, "children": [
                    (f"{orientation}.Scrollbar.thumb", {"expand": 1, "sticky": "nswe"})]})])
        style.configure("TScrollbar", troughcolor=T.BG, background="#c9d1df",
                        borderwidth=0, arrowsize=8, width=8)
        style.configure("Vertical.TScrollbar", background=T.BORDER)
        style.configure("TLabel", font=T.FONT_BODY)
        style.configure("TButton", font=T.FONT_BUTTON, padding=(7, 3), relief="flat",
                        background=T.BG, foreground=T.FG, borderwidth=1, bordercolor=T.BORDER,
                        lightcolor=T.BG, darkcolor=T.BG, focusthickness=1, focuscolor=T.ACCENT)
        style.map("TButton", background=[("active", T.ACCENT_HOVER)],
                  relief=[("pressed", "flat"), ("!pressed", "flat")])
        style.configure("Primary.TButton", background=T.ACCENT, foreground="white",
                        bordercolor=T.ACCENT)
        style.map("Primary.TButton", background=[("active", "#266bea")],
                  foreground=[("!disabled", "white")])
        for control in ("TEntry", "TSpinbox", "TCombobox"):
            style.configure(control, font=T.FONT_BODY, padding=3, relief="flat", borderwidth=1,
                            fieldbackground=T.BG, background=T.BG, foreground=T.FG,
                            bordercolor=T.BORDER, lightcolor=T.BG, darkcolor=T.BG,
                            arrowcolor=T.FG_MUTED)
            style.map(control, bordercolor=[("focus", T.ACCENT)],
                      fieldbackground=[("readonly", T.BG)])
        style.configure("TCheckbutton", font=T.FONT_UI, background=T.BG, foreground=T.FG,
                        padding=(0, 3), indicatorbackground=T.BG, indicatorforeground=T.ACCENT,
                        bordercolor=T.BORDER, lightcolor=T.BG, darkcolor=T.BG)
        style.map("TCheckbutton", background=[("active", T.BG)],
                  indicatorbackground=[("selected", T.ACCENT_HOVER)])
        # Flat checkbox images retain the native ttk state, focus and keyboard bindings.
        self._check_images = []
        for selected, disabled in ((False, False), (True, False), (False, True), (True, True)):
            photo = tk.PhotoImage(master=self, width=14, height=14)
            edge = T.GRAY if disabled else T.ACCENT if selected else "#b7c1d1"
            photo.put(edge, to=(0, 0, 14, 14))
            photo.put(edge if selected else T.BG, to=(1, 1, 13, 13))
            if selected:
                for x, y in ((3, 7), (4, 8), (5, 9), (6, 8), (7, 7), (8, 6), (9, 5), (10, 4)):
                    photo.put("white", to=(x, y, x + 1, y + 2))
            self._check_images.append(photo)
        try:
            style.element_create("V21.Check.indicator", "image", self._check_images[0],
                                 ("disabled", "selected", self._check_images[3]),
                                 ("selected", self._check_images[1]), ("disabled", self._check_images[2]))
        except tk.TclError:
            pass
        style.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("V21.Check.indicator", {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nswe"})]})]})])
        style.configure("TNotebook", background=T.BG, borderwidth=0, tabmargins=0)
        style.configure("TNotebook.Tab", font=T.FONT_MEDIUM, background=T.BG,
                        foreground=T.FG_MUTED, padding=(7, 5), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", T.SELECT), ("active", T.ACCENT_HOVER)],
                  foreground=[("selected", T.SELECT_FG)])
        style.layout("TNotebook", [("Notebook.padding", {"sticky": "nswe"})])
        style.layout("TNotebook.Tab", [("Notebook.padding", {"sticky": "nswe", "children": [
            ("Notebook.focus", {"sticky": "nswe", "children": [("Notebook.label", {"sticky": "nswe"})]})]})])

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        tasks = tk.Menu(menubar, tearoff=0)
        tasks.add_command(label="新建下载…\tCtrl+N", command=self._add_url)
        tasks.add_command(label="下载视频（选择画质）…\tCtrl+D", command=self._add_video)
        if sys.platform in ("win32", "darwin"):
            tasks.add_command(label="启动视频号下载…", command=self._start_channels)
            tasks.add_command(label="停止视频号下载", command=self._stop_channels)
        tasks.add_command(label="从剪贴板添加\tCtrl+V", command=self._paste_url)
        tasks.add_separator()
        tasks.add_command(label="安装浏览器扩展…", command=self._open_extension_help)
        tasks.add_command(label="选项…", command=self._open_settings)
        tasks.add_separator()
        if sys.platform == "win32":
            tasks.add_command(label="隐藏到托盘", command=self._hide_to_tray)
        tasks.add_command(label="退出", command=self._quit)
        menubar.add_cascade(label="任务", menu=tasks)

        downloads = tk.Menu(menubar, tearoff=0)
        downloads.add_command(label="开始 / 继续", command=self._resume_selected)
        downloads.add_command(label="暂停", command=self._pause_selected)
        downloads.add_command(label="停止 / 取消", command=self._cancel_selected)
        downloads.add_command(label="删除", command=self._delete_selected)
        downloads.add_separator()
        downloads.add_command(label="打开已下载文件", command=self._open_file)
        downloads.add_command(label="打开所在文件夹", command=self._open_folder)
        menubar.add_cascade(label="下载", menu=downloads)

        help_m = tk.Menu(menubar, tearoff=0)
        help_m.add_command(label="关于拾流下载器", command=self._about)
        menubar.add_cascade(label="帮助", menu=help_m)
        self.config(menu=menubar)

    def _build_toolbar(self, master) -> None:
        bar = tk.Frame(master, bg=T.BG_TOOLBAR, height=54)
        bar.pack(fill=tk.X)
        bar.pack_propagate(False)
        self._buttons = {}
        add = ToolbarButton(bar, "add", "添加任务", self._add_url, primary=True)
        add.pack(side=tk.LEFT, padx=(18, 0), pady=10)
        self._buttons["add"] = add
        self._add_menu = tk.Menu(self, tearoff=0)
        self._add_menu.add_command(label="添加链接…", command=self._add_url)
        self._add_menu.add_command(label="下载视频（选择画质）…", command=self._add_video)
        self._add_menu.add_command(label="从剪贴板添加", command=self._paste_url)
        if sys.platform in ("win32", "darwin"):
            self._add_menu.add_separator()
            self._add_menu.add_command(label="启动视频号下载…", command=self._start_channels)
            self._add_menu.add_command(label="停止视频号下载", command=self._stop_channels)
        drop = ToolbarButton(bar, "chevron", "", lambda: self._add_menu.tk_popup(
            drop.winfo_rootx(), drop.winfo_rooty() + drop.winfo_height()), primary=True)
        drop.pack(side=tk.LEFT, pady=10)
        options = ToolbarButton(bar, "settings", "设置", self._open_settings, outlined=True)
        options.pack(side=tk.RIGHT, padx=(9, 18), pady=10)
        self._buttons["options"] = options
        more = ToolbarButton(bar, "more", "", self._show_more, outlined=True)
        more.pack(side=tk.RIGHT, pady=10)
        self._more_button = more
        search = tk.Frame(bar, bg=T.BG_SIDEBAR, highlightbackground=T.BORDER,
                          highlightthickness=1, padx=10, pady=5)
        search.pack(side=tk.LEFT, padx=23, pady=10, fill=tk.X)
        tk.Label(search, text="⌕", bg=T.BG_SIDEBAR, fg=T.FG_MUTED,
                 font=T.FONT_SEARCH).pack(side=tk.LEFT, padx=(0, 8))
        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(search, textvariable=self.search_var, bg=T.BG_SIDEBAR,
            fg=T.FG, insertbackground=T.FG, font=T.FONT_SEARCH, width=38, bd=0, highlightthickness=0)
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.search_hint = tk.Label(search, text="搜索下载任务…", bg=T.BG_SIDEBAR,
                                    fg=T.FG_MUTED, font=T.FONT_SEARCH, cursor="xterm")
        self.search_hint.place(x=24, y=0)
        self.search_hint.bind("<Button-1>", lambda e: self.search_entry.focus_set())
        self.search_entry.bind("<FocusIn>", lambda e: self.search_hint.place_forget())
        self.search_entry.bind("<FocusOut>", lambda e: self._search_changed())
        self.search_entry.bind("<Escape>", lambda e: self.search_var.set(""))
        self.search_var.trace_add("write", lambda *a: self._search_changed())
        tk.Frame(master, bg=T.BORDER, height=1).pack(fill=tk.X)

    def _search_changed(self) -> None:
        if self.search_var.get() or self.focus_get() == self.search_entry:
            self.search_hint.place_forget()
        else:
            self.search_hint.place(x=24, y=0)
        if hasattr(self, "tree"):
            self._refresh_tree()

    def _show_more(self) -> None:
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="下载视频（选择画质）…", command=self._add_video)
        menu.add_command(label="安装浏览器扩展…", command=self._open_extension_help)
        if sys.platform in ("win32", "darwin"):
            menu.add_command(label="启动视频号下载…", command=self._start_channels)
            menu.add_command(label="停止视频号下载", command=self._stop_channels)
        menu.add_separator()
        menu.add_command(label="关于拾流下载器", command=self._about)
        menu.tk_popup(self._more_button.winfo_rootx(),
                      self._more_button.winfo_rooty() + self._more_button.winfo_height())

    def _build_body(self) -> None:
        body = self._body = tk.Frame(self, bg=T.BG)
        body.pack(fill=tk.BOTH, expand=True)

        # ── Left category sidebar (hallmark) ──
        side = tk.Frame(body, bg=T.BG_SIDEBAR, width=240)
        side.pack(side=tk.LEFT, fill=tk.Y)
        side.pack_propagate(False)

        brand = tk.Frame(side, bg=T.BG_SIDEBAR)
        brand.pack(fill=tk.X, padx=18, pady=(20, 23))
        self._brand_emblem = self._load_brand_image("logo_toolbar.png", 50)
        if self._brand_emblem is not None:
            tk.Label(brand, image=self._brand_emblem, bg=T.BG_SIDEBAR).pack(side=tk.LEFT)
        names = tk.Frame(brand, bg=T.BG_SIDEBAR)
        names.pack(side=tk.LEFT, padx=(9, 0))
        tk.Label(names, text="拾流下载器", bg=T.BG_SIDEBAR, fg=T.FG_BRAND,
                 font=T.FONT_BRAND).pack(anchor="w")
        tk.Label(names, text="让喜欢的内容，轻松带走", bg=T.BG_SIDEBAR,
                 fg=T.FG_MUTED, font=T.FONT_AUX).pack(anchor="w", pady=(5, 0))
        connection = tk.Frame(side, bg=T.BG_STATUS, padx=13, pady=11)
        connection.pack(side=tk.BOTTOM, fill=tk.X, padx=14, pady=22)
        self.browser_badge = tk.Label(connection, text="浏览器服务未启动", bg=T.BG_STATUS,
                                      fg=T.AMBER, font=T.FONT_MEDIUM, anchor="w")
        self.browser_badge.pack(fill=tk.X)
        self.browser_address = tk.Label(connection, text="127.0.0.1:7374", bg=T.BG_STATUS,
                                        fg=T.FG_MUTED, font=T.FONT_SMALL, anchor="w")
        self.browser_address.pack(fill=tk.X, pady=(5, 0))
        for w in (connection, self.browser_badge, self.browser_address):
            w.bind("<Button-1>", lambda e: self._open_extension_help())
            w.configure(cursor="hand2")
        self.cat_list = Sidebar(side, on_select=self._on_category_select)
        self.cat_list.pack(fill=tk.BOTH, expand=True, padx=11)
        self.cat_list.bind("<Button-3>", self._sidebar_context)
        self.cat_list.bind("<Button-2>", self._sidebar_context)

        # Sidebar items are built dynamically from the current categories.
        self._sidebar_items: list[tuple[str, str]] = []
        self._sidebar_sig: tuple | None = None
        self._rebuild_sidebar()

        # Vertical separator
        tk.Frame(body, bg=T.BORDER, width=1).pack(side=tk.LEFT, fill=tk.Y)

        # ── Right: list + detail ──
        right = tk.Frame(body, bg=T.BG)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._build_toolbar(right)

        # List header strip
        list_hdr = tk.Frame(right, bg=T.BG_LIST, height=32)
        list_hdr.pack(fill=tk.X)
        list_hdr.pack_propagate(False)
        self.list_title = tk.Label(
            list_hdr,
            text="全部下载",
            bg=T.BG_LIST,
            fg=T.FG_BRAND,
            font=T.FONT_TITLE,
            anchor="w",
        )
        self.list_title.pack(side=tk.LEFT, padx=(18, 14))
        self.list_count = tk.Label(
            list_hdr, text="0 项", bg=T.BG_LIST, fg=T.FG_MUTED, font=T.FONT_SMALL
        )
        self.list_count.pack(side=tk.LEFT)
        columns = ToolbarButton(list_hdr, "list", "", self._show_columns_menu, outlined=True)
        columns.pack(side=tk.RIGHT, padx=(10, 20), pady=5)
        self._columns_button = columns
        sort = ToolbarButton(list_hdr, "sort", "按添加时间", lambda: self._sort_by("date"), outlined=True)
        sort.pack(side=tk.RIGHT, pady=5)
        # Reserve the selection actions before the expanding task list.
        actionbar = tk.Frame(right, bg=T.BG_LIST, height=33)
        actionbar.pack(side=tk.BOTTOM, fill=tk.X)
        actionbar.pack_propagate(False)
        tk.Frame(actionbar, height=1, bg=T.BORDER).pack(fill=tk.X)
        self.selection_label = tk.Label(actionbar, text="已选择 0 项", bg=T.BG_LIST,
                                         fg=T.FG_MUTED, font=T.FONT_SMALL)
        self.selection_label.pack(side=tk.LEFT, padx=(18, 14))
        for key, icon, text, command in [
            ("resume", "play", "继续", self._resume_selected),
            ("pause", "pause", "暂停", self._pause_selected),
            ("stop", "stop", "停止", self._cancel_selected),
            ("delete", "trash", "删除", self._delete_selected),
            ("open", "open", "打开文件", self._open_file),
            ("folder", "folder", "打开文件夹", self._open_folder),
        ]:
            tk.Frame(actionbar, width=1, bg=T.BORDER).pack(side=tk.LEFT, fill=tk.Y, pady=12)
            button = ToolbarButton(actionbar, icon, text, command, compact=True)
            button.pack(side=tk.LEFT, padx=7, pady=5)
            self._buttons[key] = button


        # Download list
        list_wrap = tk.Frame(right, bg=T.BG_LIST)
        list_wrap.pack(fill=tk.BOTH, expand=True, padx=(11, 14))

        self.tree = ttk.Treeview(
            list_wrap,
            columns=COLUMNS,
            show="tree headings",
            selectmode="extended",
        )
        self.tree.column("#0", width=34, minwidth=34, stretch=False)
        self.tree.heading("#0", text="□", command=self._select_all)
        self._headings = {
            "filename": ("文件名", 300), "folder": ("文件夹", 210),
            "size": ("大小", 90), "status": ("状态", 100),
            "progress": ("进度", 160), "speed": ("速度", 95),
            "avg": ("平均速度", 100), "elapsed": ("已用时间", 80),
            "eta": ("预计剩余", 90), "date": ("添加时间", 105),
            "conn": ("分段", 55), "category": ("分类", 100),
        }
        for key, (label, width) in self._headings.items():
            self.tree.heading(key, text=label, command=lambda k=key: self._sort_by(k))
            stretch = key == "filename"
            self.tree.column(key, width=width, minwidth=40, stretch=stretch, anchor="w")

        # All the columns together are wider than the window, so the list needs
        # to scroll sideways — otherwise the last ones are simply unreachable.
        # The h-scrollbar must be packed first to reserve the bottom strip.
        yscroll = ttk.Scrollbar(list_wrap, orient=tk.VERTICAL, command=self.tree.yview)
        xscroll = ttk.Scrollbar(list_wrap, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.task_rows = TaskRows(self.tree, self.manager.get_job, self._context_menu)
        def scrolled(scrollbar, first, last):
            scrollbar.set(first, last)
            if scrollbar is xscroll:
                if float(first) <= 0 and float(last) >= 1:
                    scrollbar.pack_forget()
                elif not scrollbar.winfo_manager():
                    scrollbar.pack(side=tk.BOTTOM, fill=tk.X, before=self.tree)
            self.task_rows.schedule()
        self.tree.configure(yscrollcommand=lambda a, b: scrolled(yscroll, a, b),
                            xscrollcommand=lambda a, b: scrolled(xscroll, a, b))
        xscroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._col_order = self._load_column_order()
        self._visible_cols = self._load_visible_columns()
        self._apply_columns()
        self._apply_column_widths()

        # Row tags for status colors
        # Straight from the theme, so a retheme can't leave one status behind.
        for status, colour in T.STATUS_COLORS.items():
            self.tree.tag_configure(status, foreground=T.FG)
        # Composes with the status tags above rather than overriding them: those
        # set only a foreground, this sets only a background.
        self.tree.tag_configure("odd", background=T.STRIPE)

        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_selection_change())
        self.tree.bind("<Double-1>", lambda e: self._on_double_click())
        self.tree.bind("<Button-3>", self._context_menu)
        self.tree.bind("<Button-2>", self._context_menu)
        self._drag_col: str | None = None
        self._drag_resizing = False
        self.tree.bind("<ButtonPress-1>", self._heading_press, add="+")
        self.tree.bind("<ButtonRelease-1>", self._heading_release, add="+")

        self._ctx = tk.Menu(self, tearoff=0)
        self._ctx.add_command(label="开始 / 继续", command=self._resume_selected)
        self._ctx.add_command(label="暂停", command=self._pause_selected)
        self._ctx.add_command(label="停止", command=self._cancel_selected)
        self._ctx.add_command(label="重新下载", command=self._redownload_selected)
        self._ctx.add_command(label="选择画质…", command=self._choose_quality)
        self._ctx.add_command(label="显示进度窗口", command=self._show_progress_selected)
        self._ctx.add_separator()
        self._ctx.add_command(label="重命名…", command=self._rename_selected)
        self._ctx.add_command(label="移动到…", command=self._move_selected)
        self._ctx_cat = tk.Menu(self._ctx, tearoff=0)
        self._ctx.add_cascade(label="移动到分类", menu=self._ctx_cat)
        self._ctx.add_command(label="打开文件", command=self._open_file)
        self._ctx.add_command(label="打开文件夹", command=self._open_folder)
        self._ctx.add_separator()
        self._ctx.add_command(label="从列表删除", command=self._delete_selected)
        self._ctx.add_command(label="删除并移除文件", command=self._delete_with_files)

    def _select_all(self) -> None:
        rows = self.tree.get_children()
        self.tree.selection_set(()) if set(rows) == set(self.tree.selection()) else self.tree.selection_set(rows)

    def _show_columns_menu(self) -> None:
        class Position:
            x_root = self._columns_button.winfo_rootx()
            y_root = self._columns_button.winfo_rooty() + self._columns_button.winfo_height()
        self._columns_menu(Position())

    def _build_statusbar(self) -> None:
        self._status_frame = tk.Frame(self, bg=T.BG_STATUS, height=33)
        self._status_frame.pack(fill=tk.X, side=tk.BOTTOM)
        self._status_frame.pack_propagate(False)
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(
            self._status_frame,
            textvariable=self.status_var,
            bg=T.BG_STATUS,
            fg=T.FG_SUBTLE,
            font=T.FONT_SMALL,
            anchor="w",
        ).pack(side=tk.LEFT, padx=10)
        # Version + when this build was installed — a quiet build stamp, far
        # right, so it's always in view.
        self.version_lbl = tk.Label(
            self._status_frame, text=self._version_line(), bg=T.BG_STATUS,
            fg=T.FG_SUBTLE, font=T.FONT_SMALL, anchor="e",
        )
        self.version_lbl.pack(side=tk.RIGHT, padx=10)
        tk.Frame(self._status_frame, bg=T.BORDER, width=1).pack(
            side=tk.RIGHT, fill=tk.Y, pady=5)
        self.status_right = tk.Label(
            self._status_frame, text="", bg=T.BG_STATUS, fg=T.FG_SUBTLE, font=T.FONT_STATUS, anchor="e"
        )
        self.status_right.pack(side=tk.RIGHT, padx=10)

        self.speed_badge = tk.Label(self._status_frame, text="0 B/s", bg=T.BG_STATUS,
                                     fg=T.FG_SUBTLE, font=T.FONT_SMALL)
        self.speed_badge.pack(side=tk.RIGHT, padx=8)
        tk.Label(self._status_frame, text="↓", bg=T.BG_STATUS, fg=T.ACCENT,
                 font=T.FONT_UI).pack(side=tk.RIGHT)

        # Toast strip for browser captures
        self.toast_var = tk.StringVar(value="")
        self.toast_bar = tk.Label(
            self,
            textvariable=self.toast_var,
            bg=T.TOAST_BG,
            fg="white",
            font=T.FONT_MEDIUM,
            anchor="w",
            padx=12,
            pady=6,
        )

    # ── filtering ───────────────────────────────────────────────────────

    _CAT_ICONS = {
        "General": "📁", "Compressed": "📦", "Documents": "📄",
        "Music": "🎵", "Video": "🎬",
    }

    def _rebuild_sidebar(self) -> None:
        """(Re)build the sidebar so it reflects the current categories,
        including any the user added. Preserves the active filter."""
        cats = list((self.manager.settings.get("category_paths") or {}).keys())
        items: list[tuple[str, str]] = [
            ("全部下载", FILTER_ALL),
            ("下载中", FILTER_DOWNLOADING),
            ("等待中", FILTER_QUEUED),
            ("已暂停", FILTER_PAUSED),
            ("已完成", FILTER_COMPLETE),
            ("失败 / 已取消", FILTER_FAILED),
            ("文件类型", ""),
        ]
        counts: dict[str, int] = {}
        for j in self.manager.jobs:
            counts[j.category] = counts.get(j.category, 0) + 1
        order = ["Video", "Music", "Documents", "Compressed", "General"]
        cats.sort(key=lambda c: order.index(c) if c in order else len(order))
        for c in cats:
            icon = self._CAT_ICONS.get(c, "📂")
            n = counts.get(c, 0)
            label = {"Music": "音频", "General": "其他"}.get(c, CATEGORY_CN.get(c, c))
            items.append((label, FILTER_CAT_PREFIX + c))
        # Show categories that have files but aren't in category_paths.
        for c in sorted(counts):
            if c not in cats:
                items.append((CATEGORY_CN.get(c, c), FILTER_CAT_PREFIX + c))

        self._sidebar_items = items
        self._sidebar_sig = None
        status_counts = [len(self.manager.jobs),
            sum(j.status in (DownloadStatus.DOWNLOADING, DownloadStatus.CONNECTING) for j in self.manager.jobs),
            sum(j.status == DownloadStatus.QUEUED for j in self.manager.jobs),
            sum(j.status == DownloadStatus.PAUSED for j in self.manager.jobs),
            sum(j.status == DownloadStatus.COMPLETE for j in self.manager.jobs),
            sum(j.status in (DownloadStatus.FAILED, DownloadStatus.CANCELLED) for j in self.manager.jobs)]
        self.cat_list.set_items(items, status_counts + [None] + [counts.get(k[len(FILTER_CAT_PREFIX):], 0) for _, k in items[7:]])
        # Restore the selection matching the active filter.
        for i, (_lbl, key) in enumerate(items):
            if key == self._filter:
                self.cat_list.selection_clear(0, tk.END)
                self.cat_list.selection_set(i)
                break
        else:
            self.cat_list.selection_set(0)

    def _on_category_select(self, _event=None) -> None:
        sel = self.cat_list.curselection()
        if not sel:
            return
        idx = sel[0]
        _label, key = self._sidebar_items[idx]
        if not key:
            return
        self._filter = key
        # Update list title
        self.list_title.configure(text=_label.strip())
        self._refresh_tree()

    def _filtered_jobs(self) -> list[DownloadJob]:
        jobs = list(self.manager.jobs)
        query = self.search_var.get().strip().casefold() if hasattr(self, "search_var") else ""
        if query:
            jobs = [j for j in jobs if query in (j.filename + " " + j.save_path + " " + j.url).casefold()]
        f = self._filter
        if f == FILTER_ALL:
            return jobs
        if f == FILTER_DOWNLOADING:
            return [
                j
                for j in jobs
                if j.status in (DownloadStatus.DOWNLOADING, DownloadStatus.CONNECTING)
            ]
        if f == FILTER_QUEUED:
            return [j for j in jobs if j.status == DownloadStatus.QUEUED]
        if f == FILTER_PAUSED:
            return [j for j in jobs if j.status == DownloadStatus.PAUSED]
        if f == FILTER_COMPLETE:
            return [j for j in jobs if j.status == DownloadStatus.COMPLETE]
        if f == FILTER_FAILED:
            return [
                j
                for j in jobs
                if j.status in (DownloadStatus.FAILED, DownloadStatus.CANCELLED)
            ]
        if f.startswith(FILTER_CAT_PREFIX):
            cat = f[len(FILTER_CAT_PREFIX) :]
            return [j for j in jobs if j.category == cat]
        return jobs

    # ── refresh ─────────────────────────────────────────────────────────

    def _schedule_refresh(self) -> None:
        # Coalesce: never queue more than one pending refresh at a time, so a
        # burst of progress notifications from download threads can't pile up
        # thousands of after() callbacks and lock the UI.
        if getattr(self, "_refresh_pending", False):
            return
        self._refresh_pending = True
        try:
            self.after(0, self._run_scheduled_refresh)
        except tk.TclError:
            self._refresh_pending = False

    def _run_scheduled_refresh(self) -> None:
        self._refresh_pending = False
        self._refresh_all()

    def _tick(self) -> None:
        self._refresh_all()
        self.after(400, self._tick)

    def _refresh_all(self) -> None:
        self._maybe_rebuild_sidebar()
        self._refresh_tree()
        self._update_detail()
        self._update_status()
        self._update_toolbar_state()
        self._update_progress_dialogs()

    def _on_selection_change(self) -> None:
        self._update_detail()
        self._update_toolbar_state()

    def _update_toolbar_state(self) -> None:
        """Dim toolbar buttons whose action doesn't apply to the current
        selection, so a coloured button never looks active while it's inert."""
        if not getattr(self, "_buttons", None):
            return
        sel = [j for j in (self.manager.get_job(i) for i in self._selected_ids()) if j]
        S = DownloadStatus

        def any_in(*statuses) -> bool:
            return any(j.status in statuses for j in sel)

        self.selection_label.configure(text=f"已选择 {len(sel)} 项")
        self.task_rows.schedule()
        self._buttons["resume"].set_enabled(any_in(S.PAUSED, S.FAILED, S.CANCELLED, S.QUEUED))
        self._buttons["pause"].set_enabled(any_in(S.DOWNLOADING, S.CONNECTING, S.QUEUED))
        self._buttons["stop"].set_enabled(any(j.status not in (S.COMPLETE, S.CANCELLED) for j in sel))
        self._buttons["delete"].set_enabled(bool(sel))
        self._buttons["open"].set_enabled(any_in(S.COMPLETE))
        # add / video / folder / browser / options are always applicable.

    def _show_progress_selected(self) -> None:
        for jid in self._selected_ids():
            self._open_progress(jid)

    def _on_double_click(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        job = self.manager.get_job(ids[0])
        if not job:
            return
        self._open_progress(job.id)

    def _open_progress(self, job_id: str) -> None:
        """Open the detail window only when the user requests it."""
        if self.manager.get_job(job_id) is None:
            return   # stale/removed row — don't open a window that self-destructs
        existing = self._progress_dialogs.get(job_id)
        if existing is not None and existing.winfo_exists():
            existing.restore()
            return
        dlg = DownloadProgressDialog(
            self, self.manager, job_id, self._open_path,
            on_fold=(self._fold_download_to_tray if sys.platform == "win32" else None))
        self._progress_dialogs[job_id] = dlg

    def _update_progress_dialogs(self) -> None:
        for jid, dlg in list(self._progress_dialogs.items()):
            try:
                if not dlg.winfo_exists():
                    del self._progress_dialogs[jid]
                    continue
                dlg.update_view()
            except tk.TclError:
                self._progress_dialogs.pop(jid, None)
        # A folded download leaves the tray as soon as it stops running:
        #   • complete  → close its hidden window (it's done; the grid shows it)
        #   • failed / cancelled / removed → bring the window back so it isn't
        #     left stranded off-screen.
        terminal = (DownloadStatus.COMPLETE, DownloadStatus.FAILED, DownloadStatus.CANCELLED)
        changed = False
        for jid in list(self._folded_downloads):
            job = self.manager.get_job(jid)
            if job is not None and job.status not in terminal:
                continue
            self._folded_downloads.remove(jid)
            changed = True
            dlg = self._progress_dialogs.get(jid)
            if dlg is None or not dlg.winfo_exists():
                self._progress_dialogs.pop(jid, None)
            elif job is not None and job.status == DownloadStatus.COMPLETE:
                dlg._closed = True
                try:
                    dlg.destroy()
                except tk.TclError:
                    pass
                self._progress_dialogs.pop(jid, None)
            else:
                dlg.restore()   # failed/cancelled — don't leave it hidden
        # Refresh the lock-free tray snapshot each tick so folded downloads' %
        # stays current (built on the main thread; the tray side takes no lock).
        if self._folded_downloads or self._folded_snapshot:
            self._rebuild_folded_snapshot()
        if changed:
            self._refresh_tray_menu()

    def _maybe_rebuild_sidebar(self) -> None:
        cats = tuple((self.manager.settings.get("category_paths") or {}).keys())
        counts: dict[str, int] = {}
        for j in self.manager.jobs:
            counts[j.category] = counts.get(j.category, 0) + 1
        sig = (cats, tuple(sorted(counts.items())), tuple(j.status.value for j in self.manager.jobs))
        if sig != self._sidebar_sig:
            self._rebuild_sidebar()
            self._sidebar_sig = sig

    def _selected_ids(self) -> list[str]:
        return list(self.tree.selection())

    def _refresh_tree(self) -> None:
        selected = set(self.tree.selection())
        try:
            yview = self.tree.yview()
        except tk.TclError:
            yview = (0.0, 1.0)

        jobs = self._sort_jobs(self._filtered_jobs())
        existing = set(self.tree.get_children())
        job_ids = {j.id for j in jobs}

        for iid in existing - job_ids:
            self.tree.delete(iid)

        for idx, job in enumerate(jobs):
            values = self._job_row(job)
            tags = (job.status.value,) + (("odd",) if idx % 2 else ())
            if job.id in existing:
                self.tree.item(job.id, values=values, tags=tags)
            else:
                self.tree.insert("", "end", iid=job.id, values=values, tags=tags)

        for idx, job in enumerate(jobs):
            try:
                self.tree.move(job.id, "", idx)
            except tk.TclError:
                pass

        for iid in selected:
            if self.tree.exists(iid):
                self.tree.selection_add(iid)

        try:
            self.tree.yview_moveto(yview[0])
        except tk.TclError:
            pass

        self.list_count.configure(text=f"{len(jobs)} 个任务")
        self.task_rows.schedule()
        if not jobs:
            if not hasattr(self, "empty_label"):
                self.empty_label = tk.Label(self.tree, bg=T.BG_LIST, fg=T.FG_MUTED, font=T.FONT_UI)
            self.empty_label.configure(text="没有匹配的任务" if self.search_var.get() else "暂无任务 · 点击上方添加任务")
            self.empty_label.place(relx=.5, rely=.45, anchor="center")
        elif hasattr(self, "empty_label"):
            self.empty_label.place_forget()

    def _job_row(self, job: DownloadJob) -> tuple:
        if job.status == DownloadStatus.COMPLETE:
            size = format_bytes(job.total_size or job.downloaded)
        elif job.total_size:
            size = f"{format_bytes(job.downloaded)} / {format_bytes(job.total_size)}"
        elif job.downloaded:
            size = format_bytes(job.downloaded)
        else:
            size = "未知"

        status = STATUS_CN.get(job.status.value, job.status.value)
        if job.status == DownloadStatus.FAILED and job.error:
            status = "失败"
        elif job.status == DownloadStatus.PROCESSING:
            status = "合并中…"

        if job.is_stream and job.media_meta.get("seg_total"):
            pct = job.progress
            filled = int(pct / 10)
            bar = "█" * filled + "░" * (10 - filled)
            progress = f"{bar} {pct:.0f}%"
        elif job.total_size:
            # Visual mini bar in text
            pct = job.progress
            filled = int(pct / 10)
            bar = "█" * filled + "░" * (10 - filled)
            progress = f"{bar} {pct:.0f}%"
        elif job.downloaded:
            progress = format_bytes(job.downloaded)
        else:
            progress = "—"

        speed = (
            format_speed(job.speed_bps)
            if job.status == DownloadStatus.DOWNLOADING
            else "—"
        )
        # Unlike Speed, this stays put once the download stops — it's the whole
        # point of the column. Jobs from before active_seconds was tracked have
        # nothing to average, so they read "—".
        avg = format_speed(job.avg_speed_bps)
        # Time spent actually downloading, so a job paused overnight doesn't
        # claim a 9-hour "elapsed".
        elapsed = format_eta(job.active_seconds) if job.active_seconds > 0 else "—"
        # When it finished, falling back to when it was added for anything that
        # hasn't finished. Sorts correctly as text, which is how the grid sorts.
        stamp = job.created_at
        date = time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) if stamp else "—"
        eta = (
            format_eta(job.eta_seconds)
            if job.status == DownloadStatus.DOWNLOADING
            else "—"
        )
        if job.is_stream:
            conn = job.media_type.upper()
        else:
            conn = str(job.connections) if job.supports_ranges else "1"
        try:
            folder = str(migrated_path(job.save_path).parent)
        except Exception:
            folder = ""
        return (job.filename, folder, size, status, progress, speed, avg, elapsed,
                eta, date, conn, CATEGORY_CN.get(job.category, job.category))

    def _update_detail(self) -> None:
        # Task details remain available through the existing double-click dialog.
        self.task_rows.schedule()

    def _update_status(self) -> None:
        jobs = self.manager.jobs
        active = sum(
            1
            for j in jobs
            if j.status in (DownloadStatus.DOWNLOADING, DownloadStatus.CONNECTING)
        )
        complete = sum(1 for j in jobs if j.status == DownloadStatus.COMPLETE)
        total_speed = sum(
            j.speed_bps
            for j in jobs
            if j.status == DownloadStatus.DOWNLOADING
        )
        self.speed_badge.configure(text=format_speed(total_speed) if total_speed else "0 B/s")
        self.status_var.set(
            f"总任务：{len(jobs)}   ·   进行中：{active}   ·   已完成：{complete}   ·   "
            f"同时下载：{self.manager.settings.get('max_simultaneous', 3)}"
        )
        port = self.manager.settings.get("browser_port", 7374)
        if self._browser and self._browser.running:
            browser_txt = f"浏览器连接：127.0.0.1:{port}"
            self.browser_badge.configure(text="●  浏览器服务就绪", fg=T.SPEED_BADGE)
        else:
            err = (self._browser.last_error if self._browser else "") or "disabled"
            browser_txt = f"浏览器连接：关闭（{err}）"
            self.browser_badge.configure(text="○  浏览器服务未启动", fg=T.AMBER)
        self.browser_address.configure(text=f"127.0.0.1:{port}")
        self.status_right.configure(text=f"连接数：{self.manager.settings.get('connections', 8)}")

    def _sort_key(self, job: DownloadJob, col: str):
        """Sort on the underlying value, not the text in the cell.

        Sorting the displayed strings put "9m 30s" after "10m 00s" and 900 KB/s
        above 5 MB/s. Every branch returns one consistent type per column so the
        keys stay comparable.
        """
        if col == "size":
            return float(job.total_size or job.downloaded or 0)
        if col == "progress":
            return job.progress
        if col == "speed":
            return job.speed_bps
        if col == "avg":
            return job.avg_speed_bps
        if col == "elapsed":
            return job.active_seconds
        if col == "eta":
            eta = job.eta_seconds
            return eta if eta is not None else float("inf")   # unknown sorts last
        if col == "date":
            return float(job.created_at or 0.0)
        if col == "conn":
            return float(job.connections)
        if col == "folder":
            try:
                return str(migrated_path(job.save_path).parent).lower()
            except Exception:  # noqa: BLE001
                return ""
        if col == "status":
            return job.status.value.lower()
        if col == "category":
            return job.category.lower()
        return job.filename.lower()

    def _sort_jobs(self, jobs: list[DownloadJob]) -> list[DownloadJob]:
        if not self._sort_col:
            return jobs
        return sorted(jobs, key=lambda j: self._sort_key(j, self._sort_col),
                      reverse=self._sort_reverse)

    def _update_sort_indicators(self) -> None:
        for key, (label, _w) in self._headings.items():
            arrow = ""
            if key == self._sort_col:
                arrow = "  ▼" if self._sort_reverse else "  ▲"
            self.tree.heading(key, text=label + arrow)

    def _sort_by(self, col: str) -> None:
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = False
        self._update_sort_indicators()
        # Sorting the tree directly was pointless: _refresh_tree moves every row
        # back into the manager's order, and it runs on every progress tick — so
        # the sort visibly reverted within a second. The order has to come from
        # the job list the refresh renders.
        self._refresh_tree()

    # ── column layout: order, visibility, widths ────────────────
    #
    # Two separate things, kept apart on purpose: _col_order is where every
    # column sits (including hidden ones), _visible_cols is which are shown.
    # Keeping the order for hidden columns means re-showing one puts it back
    # where it was, instead of teleporting it to the end.

    def _load_visible_columns(self) -> list[str]:
        """Saved visible set, filtered to columns that still exist.

        Empty/absent means "show everything" — so columns added by a later
        version appear for anyone who never customised the list.
        """
        saved = [c for c in (self.manager.settings.get("visible_columns") or [])
                 if c in COLUMNS]
        return saved or ["filename", "status", "progress", "size", "speed", "eta", "date"]

    def _load_column_order(self) -> list[str]:
        """Saved order, plus any column the saved order predates."""
        saved = [c for c in (self.manager.settings.get("column_order") or [])
                 if c in COLUMNS]
        return saved + [c for c in ("filename", "status", "progress", "size", "speed", "eta", "date", "folder", "avg", "elapsed", "conn", "category") if c not in saved]

    def _apply_columns(self) -> None:
        cols = [c for c in self._col_order if c in self._visible_cols]
        if ALWAYS_SHOWN not in cols:
            cols.insert(0, ALWAYS_SHOWN)
        self._visible_cols = cols
        self.tree.configure(displaycolumns=cols)
        self._col_vars = {c: tk.BooleanVar(value=c in cols) for c in COLUMNS}

    def _apply_visible_columns(self, visible: list[str]) -> None:
        self._visible_cols = list(visible)
        if not getattr(self, "_col_order", None):
            self._col_order = list(COLUMNS)
        self._apply_columns()

    def _apply_column_widths(self) -> None:
        saved = self.manager.settings.get("column_widths") or {}
        for key, (_label, default) in self._headings.items():
            try:
                self.tree.column(key, width=int(saved.get(key, default)))
            except (tk.TclError, TypeError, ValueError):
                pass

    def _save_columns(self) -> None:
        s = self.manager.settings
        s["column_order"] = list(self._col_order)
        s["visible_columns"] = list(self._visible_cols)
        widths = {}
        for key in COLUMNS:
            try:
                widths[key] = int(self.tree.column(key, "width"))
            except (tk.TclError, TypeError, ValueError):
                pass
        # Hidden columns report a stale width; keep what was saved for them.
        old = self.manager.settings.get("column_widths") or {}
        for key in COLUMNS:
            if key not in self._visible_cols and key in old:
                widths[key] = old[key]
        s["column_widths"] = widths
        self.manager.save_settings()

    def _toggle_column(self, key: str) -> None:
        visible = [c for c in COLUMNS if self._col_vars[c].get()]
        if not visible:                      # unticked the last one — undo it
            self._col_vars[key].set(True)
            return
        self._visible_cols = visible
        self._apply_columns()
        self._save_columns()

    def _show_all_columns(self) -> None:
        self._visible_cols = list(COLUMNS)
        self._apply_columns()
        self._save_columns()

    def _reset_columns(self) -> None:
        self._col_order = ["filename", "status", "progress", "size", "speed", "eta", "date", "folder", "avg", "elapsed", "conn", "category"]
        self._visible_cols = ["filename", "status", "progress", "size", "speed", "eta", "date"]
        self._apply_columns()
        for key, (_label, default) in self._headings.items():
            try:
                self.tree.column(key, width=default)
            except tk.TclError:
                pass
        self.manager.settings["column_widths"] = {}
        self._save_columns()

    # ── drag a heading to move a column ─────────────────────────
    #
    # ttk::treeview can't reorder columns itself, so this does it. Releasing
    # over a *different* heading fires no heading command, so a drag can't be
    # mistaken for a click-to-sort — verified against Tk 8.6.

    def _heading_press(self, event: tk.Event) -> None:
        self._drag_col = None
        self._drag_resizing = False
        region = self.tree.identify_region(event.x, event.y)
        if region == "separator":
            self._drag_resizing = True       # a width drag; save it on release
            return
        if region != "heading":
            return
        try:
            self._drag_col = self.tree.column(self.tree.identify_column(event.x), "id")
        except tk.TclError:
            self._drag_col = None

    def _heading_release(self, event: tk.Event) -> None:
        if self._drag_resizing:
            self._drag_resizing = False
            self._save_columns()             # remember the new width
            return
        src, self._drag_col = self._drag_col, None
        if not src or self.tree.identify_region(event.x, event.y) != "heading":
            return
        try:
            dst = self.tree.column(self.tree.identify_column(event.x), "id")
        except tk.TclError:
            return
        if not dst or dst == src:
            return                           # a plain click — that's the sort
        order = [c for c in self._col_order if c != src]
        order.insert(order.index(dst), src)
        self._col_order = order
        self._apply_columns()
        self._save_columns()

    def _columns_menu(self, event: tk.Event) -> None:
        m = tk.Menu(self, tearoff=0)
        for key in COLUMNS:
            label = self._headings[key][0]
            if key == ALWAYS_SHOWN:
                m.add_checkbutton(label=label, variable=self._col_vars[key],
                                  state="disabled")
                continue
            m.add_checkbutton(label=label, variable=self._col_vars[key],
                              command=lambda k=key: self._toggle_column(k))
        m.add_separator()
        m.add_command(label="显示全部列", command=self._show_all_columns)
        m.add_command(label="重置列", command=self._reset_columns)
        m.tk_popup(event.x_root, event.y_root)

    def _context_menu(self, event: tk.Event) -> None:
        # Right-clicking the header picks columns; right-clicking a row acts on
        # the download.
        if self.tree.identify_region(event.x, event.y) in ("heading", "separator"):
            self._columns_menu(event)
            return
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self._sync_category_menu()
            self._ctx.tk_popup(event.x_root, event.y_root)

    def _sync_category_menu(self) -> None:
        """Rebuild the 'Move to category' submenu from the current categories."""
        self._ctx_cat.delete(0, tk.END)
        cats = list((self.manager.settings.get("category_paths") or {}).keys())
        if not cats:
            self._ctx_cat.add_command(label="(no categories)", state="disabled")
            return
        for c in cats:
            icon = self._CAT_ICONS.get(c, "📂")
            self._ctx_cat.add_command(
                label=f"{icon}  {c}", command=lambda cc=c: self._move_selected_to_category(cc)
            )

    def _move_selected_to_category(self, category: str) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        failed = []
        for jid in ids:
            ok, err = self.manager.move_to_category(jid, category)
            if not ok:
                job = self.manager.get_job(jid)
                failed.append(f"{job.filename if job else jid}: {err}")
        if failed:
            messagebox.showerror("Move to category", "\n".join(failed[:5]), parent=self)
        self._refresh_all()

    # ── sidebar (category folders) right-click ───────────────────────────

    def _sidebar_context(self, event: tk.Event) -> None:
        idx = self.cat_list.nearest(event.y)
        if idx < 0 or idx >= len(self._sidebar_items):
            return
        label, key = self._sidebar_items[idx]
        menu = tk.Menu(self, tearoff=0)
        is_cat = bool(key) and key.startswith(FILTER_CAT_PREFIX)
        cat = key[len(FILTER_CAT_PREFIX):] if is_cat else ""
        if is_cat:
            # Select + switch to the category so the menu targets what's shown.
            self.cat_list.selection_clear(0, tk.END)
            self.cat_list.selection_set(idx)
            self._on_category_select()
            menu.add_command(label="打开文件夹", command=lambda c=cat: self._sidebar_browse(c))
            menu.add_separator()
        menu.add_command(label="添加分类…", command=self._sidebar_add_category)
        if is_cat:
            builtin = cat in self.manager.BUILTIN_CATEGORIES
            menu.add_command(
                label="删除分类",
                state="disabled" if builtin else "normal",
                command=lambda c=cat: self._sidebar_delete_category(c),
            )
        menu.tk_popup(event.x_root, event.y_root)

    def _sidebar_browse(self, cat: str) -> None:
        folder = (self.manager.settings.get("category_paths") or {}).get(cat)
        if not folder:
            folder = str(default_download_dir(self.manager.settings))
        p = migrated_path(folder)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._open_path(p)

    def _sidebar_add_category(self) -> None:
        name = messagebox.askstring("添加分类", "分类名称：", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        folder = filedialog.askdirectory(
            title=f"“{name}”的文件夹（取消则使用默认位置）",
            initialdir=self.manager.settings.get("default_save_path") or None,
            parent=self,
            mustexist=False,
        ) or None
        created = self.manager.add_category(name, folder)
        if created:
            self._rebuild_sidebar()

    def _sidebar_delete_category(self, cat: str) -> None:
        if not messagebox.askyesno(
            "删除分类",
            f"从侧边栏移除“{cat}”分类？\n\n"
            "已下载的文件会保留，仅移除分类。",
            parent=self,
        ):
            return
        if self.manager.remove_category(cat):
            if self._filter == FILTER_CAT_PREFIX + cat:
                self._filter = FILTER_ALL
            self._rebuild_sidebar()
            self._refresh_tree()
        else:
            messagebox.showinfo(
                "删除分类", "内置分类不能删除。", parent=self
            )

    # ── file-table right-click file operations ───────────────────────────

    def _rename_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        job = self.manager.get_job(ids[0])
        if not job:
            return
        new = messagebox.askstring(
            "重命名", "新文件名：", parent=self, initialvalue=job.filename
        )
        if not new or new.strip() == job.filename:
            return
        ok, err = self.manager.rename_job(job.id, new)
        if not ok:
            messagebox.showerror("重命名", err, parent=self)
        self._refresh_all()

    def _move_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        job = self.manager.get_job(ids[0])
        if not job:
            return
        dest = filedialog.askdirectory(
            title="移动到文件夹",
            initialdir=str(migrated_path(job.save_path).parent),
            parent=self,
            mustexist=False,
        )
        if not dest:
            return
        ok, err = self.manager.move_job(job.id, dest)
        if not ok:
            messagebox.showerror("移动", err, parent=self)
        self._refresh_all()

    def _redownload_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        names = [j.filename for j in (self.manager.get_job(i) for i in ids) if j]
        if not names:
            return
        preview = ", ".join(names[:5]) + (" …" if len(names) > 5 else "")
        if not messagebox.askyesno(
            "重新下载",
            f"从头重新下载并丢弃当前进度？\n\n{preview}",
            parent=self,
        ):
            return
        for jid in ids:
            self.manager.redownload_job(jid)
        self._refresh_all()

    # ── actions ─────────────────────────────────────────────────────────

    def _add_url(self, initial: str = "") -> None:
        def on_submit(job: DownloadJob) -> None:
            start = getattr(job, "_start_immediately", True)
            self.manager.add_job(job, start=start)
            self._filter = FILTER_ALL
            self.cat_list.selection_clear(0, tk.END)
            self.cat_list.selection_set(0)
            self.list_title.configure(text="全部下载")
            self._refresh_all()

        AddDownloadDialog(self, self.manager.settings, on_submit, initial_url=initial)

    def _paste_url(self) -> None:
        try:
            clip = self.clipboard_get().strip()
        except tk.TclError:
            clip = ""
        if clip and urlparse(clip).scheme in ("http", "https"):
            self._add_url(initial=clip)
        else:
            self._add_url()

    def _add_video(self, initial_url: str = "") -> None:
        def probe(url: str) -> dict:
            return self.manager.probe_video(url)

        def on_submit(url: str, folder: str, sel: dict, media_type: str, title: str, category: str = "") -> None:
            mt = media_type if media_type in ("page", "hls", "dash") else "page"
            job = self.manager.add_video_job(url, mt, sel, title=title, folder=folder, start=True, category=category or None)
            self._remember_save_dir(folder)
            self._filter = FILTER_ALL
            self.cat_list.selection_clear(0, tk.END)
            self.cat_list.selection_set(0)
            self._refresh_all()

        if not initial_url:
            try:
                clip = self.clipboard_get().strip()
                if urlparse(clip).scheme in ("http", "https"):
                    initial_url = clip
            except tk.TclError:
                pass
        AddVideoDialog(self, self.manager.settings, on_submit, probe, initial_url=initial_url,
                       add_category=self.manager.add_category)

    def _show_capture_dialog(self, spec: dict) -> None:
        # One dialog per captured file, shown one at a time.
        self._capture_queue.append(spec)
        self._pump_capture_queue()

    def _pump_capture_queue(self) -> None:
        if self._capture_active or not self._capture_queue:
            return
        spec = self._capture_queue.pop(0)
        self._capture_active = True

        # Bring the app forward so the dialog is visible over the browser.
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.after(300, lambda: self.attributes("-topmost", False))
        except tk.TclError:
            pass

        def on_result(final: dict, start: bool, always: bool) -> None:
            if always != bool(self.manager.settings.get("confirm_browser_captures", True)):
                self.manager.settings["confirm_browser_captures"] = always
                self.manager.save_settings()
            res = self.manager.add_capture_confirmed(
                url=final["url"], filename=final["filename"], folder=final["folder"],
                category=final["category"], connections=final["connections"],
                media_type=final["media_type"], media_meta=final["media_meta"],
                cookie=final["cookie"], referrer=final["referrer"],
                extra_headers=final["extra_headers"], start=start, source="browser",
                overwrite=bool(final.get("overwrite")),
            )
            self._remember_save_dir(final["folder"])
            self._filter = FILTER_ALL
            self.cat_list.selection_clear(0, tk.END)
            self.cat_list.selection_set(0)
            self.list_title.configure(text="全部下载")
            self._refresh_all()

        def on_closed() -> None:
            self._capture_active = False
            # Show the next queued capture, if any.
            self.after(100, self._pump_capture_queue)

        CaptureDialog(
            self, self.manager.settings, spec, on_result,
            add_category=self.manager.add_category, on_closed=on_closed,
        )

    def _remember_save_dir(self, folder: str) -> None:
        folder = (folder or "").strip()
        if folder and folder != self.manager.settings.get("last_save_dir"):
            self.manager.settings["last_save_dir"] = folder
            self.manager.save_settings()

    def _choose_quality(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        job = self.manager.get_job(ids[0])
        if not job:
            return
        # Prefer the original page URL for stream/page jobs.
        url = job.media_meta.get("page_url") or job.url

        def probe(u: str) -> dict:
            mt = job.media_type if job.media_type in ("page", "hls", "dash") else ""
            return self.manager.probe_video(u, mt, cookie=job.cookie, referrer=job.referrer)

        def on_submit(u: str, folder: str, sel: dict, media_type: str, title: str, category: str = "") -> None:
            self.manager.set_job_quality(job.id, sel, title=title)
            self._refresh_all()

        AddVideoDialog(
            self, self.manager.settings, on_submit, probe,
            initial_url=url, submit_label="重新下载",
            add_category=self.manager.add_category,
        )

    def _resume_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("继续下载", "请先选择一个或多个下载任务。", parent=self)
            return
        for jid in ids:
            self.manager.retry_job(jid)

    def _pause_selected(self) -> None:
        for jid in self._selected_ids():
            self.manager.pause_job(jid)

    def _cancel_selected(self) -> None:
        for jid in self._selected_ids():
            self.manager.cancel_job(jid)

    def _log_tk_exception(self, exc, val, tb) -> None:
        """Record any exception raised inside a Tk callback to error.log — the
        frozen app has no console, so these would otherwise vanish silently."""
        try:
            import datetime
            import traceback
            from magic_downloader.paths import DATA_DIR
            with open(DATA_DIR / "error.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}]\n")
                f.write("".join(traceback.format_exception(exc, val, tb)))
        except Exception:
            pass

    def _log_runtime_paths(self) -> None:
        """Record resolved bundle and external-tool paths for Finder launches."""
        try:
            from magic_downloader.media.ffmpeg import find_ffmpeg
            from magic_downloader.media.youtube_browser import find_node
            from magic_downloader.paths import DATA_DIR, DATA_ROOT, RESOURCE_ROOT

            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(DATA_DIR / "runtime_paths.log", "a", encoding="utf-8") as f:
                f.write(f"Resources: {RESOURCE_ROOT}\n")
                f.write(f"Application Support: {DATA_ROOT}\n")
                f.write(f"Node: {find_node() or 'not found'}\n")
                f.write(f"ffmpeg: {find_ffmpeg() or 'not found'}\n")
        except Exception:
            pass

    def _delete_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        # Unfinished downloads leave a .part (and streams a segment folder) that
        # only this app can resume — and the record needed to resume them is
        # what's about to be removed. Deleting that silently would throw away
        # hours of transfer on a bare Delete keypress; leaving it silently
        # orphans gigabytes the user never sees. So say what's there and ask.
        leftover = self.manager.leftover_bytes(ids)
        purge = True
        if leftover > 0:
            answer = messagebox.ask(
                "删除",
                f"从列表移除 {len(ids)} 项？\n\n"
                f"磁盘上有 {format_bytes(leftover)} 未完成数据。"
                f"移除后将无法继续这些任务。",
                buttons=[
                    ("移除并删除数据", "purge"),
                    ("移除但保留数据", "keep"),
                    ("取消", "cancel"),
                ],
                parent=self,
            )
            if answer in (None, "cancel"):
                return
            purge = answer == "purge"
        elif self.manager.settings.get("confirm_delete", True) and not messagebox.askyesno(
            "删除", f"从列表移除 {len(ids)} 项？", parent=self
        ):
            return
        for jid in ids:
            self.manager.delete_job(jid, delete_files=False, delete_partial=purge)

    def _delete_with_files(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        if not messagebox.askyesno(
            "删除文件",
            f"移除 {len(ids)} 项并删除磁盘上的部分及完整文件？",
            parent=self,
        ):
            return
        for jid in ids:
            self.manager.delete_job(jid, delete_files=True)

    def _open_file(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        job = self.manager.get_job(ids[0])
        if not job:
            return
        path = migrated_path(job.save_path)
        if not path.exists():
            messagebox.showinfo(
                "打开文件",
                "文件尚未生成，可能仍在下载或已被移除。",
                parent=self,
            )
            return
        self._open_path(path)

    def _open_folder(self) -> None:
        ids = self._selected_ids()
        if not ids:
            # Open default downloads folder
            folder = default_download_dir(self.manager.settings)
            folder.mkdir(parents=True, exist_ok=True)
            self._open_path(folder)
            return
        job = self.manager.get_job(ids[0])
        if not job:
            return
        folder = migrated_path(job.save_path).parent
        folder.mkdir(parents=True, exist_ok=True)
        self._open_path(folder)

    def _open_path(self, path: Path) -> None:
        path = migrated_path(path)
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except OSError as exc:
            messagebox.showerror("打开", str(exc), parent=self)

    def _open_settings(self) -> None:
        def on_save(settings: dict) -> None:
            old_port = self.manager.settings.get("browser_port")
            old_on = self.manager.settings.get("browser_integration", True)
            old_video_dir = (
                self._channels_download_dir()
                if self._channels is not None and self._channels.running
                else None
            )
            self.manager.settings.update(settings)
            self.manager.save_settings()
            if self._channels is not None and self._channels.running and old_video_dir != self._channels_download_dir():
                try:
                    self._channels.stop()
                    self._channels.start(self._channels_download_dir())
                    self._show_toast("视频号保存目录已同步")
                except Exception as exc:
                    messagebox.showerror("视频号下载", f"保存目录切换失败：{exc}", parent=self)
            # Restart browser API if settings changed
            new_port = settings.get("browser_port")
            new_on = settings.get("browser_integration", True)
            if old_port != new_port or old_on != new_on or settings.get("browser_token") is not None:
                self._restart_browser_server()
            self._update_status()

        SettingsDialog(self, self.manager.settings, on_save)

    def _extension_dir(self) -> Path:
        from magic_downloader.paths import extension_dir

        return extension_dir()

    def _open_extension_help(self) -> None:
        ext = self._extension_dir()
        if not ext.is_dir() or not (ext / "manifest.json").is_file():
            messagebox.showerror(
                "浏览器扩展不可用",
                f"扩展文件夹或 manifest.json 不存在：\n{ext}",
                parent=self,
            )
            return
        port = int(self.manager.settings.get("browser_port") or 7374)
        running = bool(self._browser and self._browser.running)
        msg = (
            "网页上没有出现下载按钮？\n"
            "────────────────────────────────────────────\n"
            "请先安装随软件附带的浏览器扩展。\n\n"
            f"浏览器连接：{'已启动' if running else '未启动'}（端口 {port}）\n\n"
            "安装步骤：\n"
            "1. 保持本软件运行\n"
            "2. 打开 chrome://extensions（Edge 用 edge://extensions）\n"
            "3. 打开右上角的‘开发者模式’\n"
            "4. 点击‘加载已解压的扩展程序’，选择文件夹：\n\n"
            f"   {ext}\n\n"
            "播放视频后，点击播放器附近的‘下载视频’按钮。\n"
            "弹窗会先显示推荐画质，其他资源可按需展开。\n\n"
            "高清音视频合并需要 ffmpeg，可在‘选项 → 视频’里安装。\n\n"
            "现在打开扩展文件夹吗？"
        )
        if messagebox.askyesno("安装浏览器扩展", msg, parent=self):
            self._open_path(ext)
            # Also open the bundled guide.
            from magic_downloader.paths import install_txt_path

            guide = install_txt_path()
            if guide.exists():
                try:
                    self._open_path(guide)
                except OSError:
                    pass

    def _channels_download_dir(self) -> Path:
        return migrated_path(
            self.manager.settings.get("category_paths", {}).get("Video")
            or default_download_dir(self.manager.settings)
        )

    def _start_channels(self) -> None:
        """Enable the bundled WeChat desktop video-channel workflow on demand."""
        if self._channels is None:
            return
        if self._channels.running:
            messagebox.showinfo(
                "视频号下载",
                "视频号下载已启用。回到微信桌面版，打开并播放视频，"
                "在右下角“下载”菜单中选择原始视频或画质。",
                parent=self,
            )
            return
        platform_name = "macOS 登录钥匙串" if sys.platform == "darwin" else "Windows 用户"
        if not messagebox.askyesno(
            "启动视频号下载",
            f"拾流下载器会临时启用本地代理，并为当前 {platform_name} 添加短时本地证书，"
            "让微信桌面端视频号显示下载菜单。\n\n"
            "停止视频号下载或退出拾流下载器时，会恢复原代理并移除该证书。现在启动吗？",
            parent=self,
        ):
            return
        folder = self._channels_download_dir()
        try:
            self._channels.start(folder)
        except Exception as exc:  # noqa: BLE001 - show the actionable startup error
            messagebox.showerror("视频号下载", str(exc), parent=self)
            return
        self._show_toast("视频号下载已启用")
        messagebox.showinfo(
            "视频号下载已启用",
            f"保存位置：\n{folder}\n\n"
            "回到微信桌面版，重新打开并播放视频。右下角会出现“下载”；"
            "选择“原始视频”或所需画质即可。下载进度显示在微信右上角 Downloads 面板。",
            parent=self,
        )

    def _stop_channels(self) -> None:
        if self._channels is None:
            return
        if not self._channels.running:
            return
        try:
            self._channels.stop()
        except Exception as exc:
            messagebox.showerror("视频号下载", str(exc), parent=self)
            return
        self._show_toast("视频号下载已停止，原代理已恢复")

    def _show_toast(self, text: str) -> None:
        self.toast_var.set(f"  🌐  {text}")
        try:
            self.toast_bar.pack_forget()
        except tk.TclError:
            pass
        try:
            self.toast_bar.pack(fill=tk.X, side=tk.BOTTOM, before=self._status_frame)
        except Exception:
            self.toast_bar.pack(fill=tk.X, side=tk.BOTTOM)
        if self._toast_after:
            try:
                self.after_cancel(self._toast_after)
            except Exception:
                pass
        self._toast_after = self.after(4500, self._hide_toast)

    def _hide_toast(self) -> None:
        try:
            self.toast_bar.pack_forget()
        except tk.TclError:
            pass
        self.toast_var.set("")

    def _start_browser_server(self) -> None:
        if not self.manager.settings.get("browser_integration", True):
            return
        port = int(self.manager.settings.get("browser_port") or 7374)
        token = str(self.manager.settings.get("browser_token") or "")

        def on_add(data: dict) -> dict:
            spec = self.manager.suggest_capture(data)
            # "Ask each time" quality: a video/stream arrived without an explicit
            # quality pick — pop the quality picker instead of auto-grabbing it.
            if spec.get("is_stream") and (spec.get("media_meta") or {}).get("ask_quality"):
                try:
                    self.after(0, lambda u=spec["url"]: self._add_video(u))
                    return {"prompted": True, "filename": spec["filename"], "media_type": spec["media_type"]}
                except tk.TclError:
                    pass   # headless — fall through to a normal add
            #: pop the "Download File Info" dialog unless disabled.
            if self.manager.settings.get("confirm_browser_captures", True):
                try:
                    self.after(0, lambda: self._show_capture_dialog(spec))
                except tk.TclError:
                    return self.manager.add_from_browser(data)
                return {"prompted": True, "filename": spec["filename"], "media_type": spec["media_type"]}

            result = self.manager.add_from_browser(data)
            name = result.get("filename") or "file"
            try:
                self.after(0, lambda: self._show_toast(f"Captured from browser: {name}"))
                self.after(0, self._refresh_all)
            except tk.TclError:
                pass
            return result

        def on_status() -> dict:
            snap = self.manager.status_snapshot()
            snap["port"] = port
            return snap

        def on_probe(data: dict) -> dict:
            url = str(data.get("url") or "")
            mtype = str(data.get("media_type") or "").lower()
            ua = str(data.get("user_agent") or "")
            if not (ua.startswith("Mozilla/") and len(ua) < 512 and "\n" not in ua and "\r" not in ua):
                ua = str(self.manager.settings.get("user_agent") or "")
            cookie = str(data.get("cookie") or "")
            referrer = str(data.get("referrer") or data.get("page_url") or "")
            if mtype == "page":
                from magic_downloader.media.ytdlp_engine import probe_formats

                return probe_formats(url=url, cookie=cookie, user_agent=ua, referrer=referrer)
            from magic_downloader.media.probe import probe_media

            return probe_media(
                url=url,
                media_type=mtype or None,
                cookie=cookie,
                referrer=referrer,
                user_agent=ua,
            )

        self._browser = BrowserAPIServer(
            port=port, on_add=on_add, on_status=on_status, token=token, on_probe=on_probe,
            on_route=self.manager.preview_capture_destination,
        )
        try:
            self._browser.start()
        except OSError as exc:
            self._browser.last_error = str(exc)
            # Don't crash the app if the port is busy
            # Capture the message now — exception vars are cleared after the
            # except block, and the lambda runs later on the Tk event loop.
            msg = (
                f"无法在端口 {port} 启动浏览器连接：\n{exc}\n\n"
                "请在选项中更换端口，或关闭占用该端口的软件。"
            )
            try:
                self.after(
                    200,
                    lambda: messagebox.showwarning(
                        "浏览器连接",
                        msg,
                        parent=self,
                    ),
                )
            except tk.TclError:
                pass

    def _restart_browser_server(self) -> None:
        if self._browser:
            self._browser.stop()
            self._browser = None
        self._start_browser_server()

    def _record_version(self) -> None:
        """Stamp when this version was first launched, so About / the status bar
        can show "updated <date>". Fires once per version (including the first
        install, and after every in-app update, which relaunches the app)."""
        from magic_downloader import __version__

        s = self.manager.settings
        if s.get("installed_version") != __version__:
            s["installed_version"] = __version__
            s["updated_at"] = time.time()
            try:
                self.manager.save_settings()
            except Exception:  # noqa: BLE001
                pass

    def _version_line(self, long: bool = False) -> str:
        from magic_downloader import __version__

        ts = self.manager.settings.get("updated_at") or 0
        if ts:
            fmt = "%Y-%m-%d %H:%M" if long else "%b %d, %Y"
            return f"v{__version__}" if not long else f"v{__version__} · 更新于 {time.strftime(fmt, time.localtime(ts))}"
        return f"v{__version__}"

    def _about(self) -> None:
        from magic_downloader import __version__
        AboutDialog(self, __version__, logo=getattr(self, "_brand_emblem", None))

    # ── system tray (: close hides, only Exit quits) ────────────

    def _load_brand_image(self, filename: str, height: int, fallbacks=()):
        """Load + scale a bundled brand image (RESOURCE_ROOT/<filename>) to the
        given height, trimming transparent margins. ``fallbacks`` are extra
        paths (relative to the extension dir) to try. PhotoImage, or None."""
        try:
            from PIL import Image, ImageTk

            from magic_downloader.paths import RESOURCE_ROOT, extension_dir

            candidates = [RESOURCE_ROOT / filename]
            for fb in fallbacks:
                candidates.append(extension_dir() / fb)
                candidates.append(RESOURCE_ROOT / "browser_extension" / fb)
            for p in candidates:
                try:
                    if p.exists():
                        im = Image.open(p).convert("RGBA")
                        box = im.split()[3].point(lambda a: 255 if a > 25 else 0).getbbox()
                        if box:
                            im = im.crop(box)
                        w = max(1, round(im.width * height / im.height))
                        im = im.resize((w, height), Image.LANCZOS)
                        return ImageTk.PhotoImage(im, master=self)
                except Exception:  # noqa: BLE001 — try the next candidate
                    continue
        except Exception:  # noqa: BLE001 — PIL missing etc. → text fallback
            return None
        return None

    def _set_window_icon(self) -> None:
        """Use the confirmed app artwork for native windows and child dialogs."""
        try:
            from PIL import Image, ImageTk
            from magic_downloader.paths import RESOURCE_ROOT
            image = Image.open(RESOURCE_ROOT / "logo_toolbar.png").convert("RGBA")
            self._window_photo = ImageTk.PhotoImage(image.resize((128,128),Image.Resampling.LANCZOS), master=self)
            self.iconphoto(True, self._window_photo)
        except (OSError, tk.TclError):
            pass

    def _tray_image(self):
        from PIL import Image

        from magic_downloader.paths import RESOURCE_ROOT, extension_dir

        img = None
        for p in (
            extension_dir() / "icons" / "icon128.png",
            RESOURCE_ROOT / "browser_extension" / "icons" / "icon128.png",
        ):
            try:
                if p.exists():
                    img = Image.open(p).convert("RGBA")
                    break
            except Exception:
                pass
        if img is None:
            img = Image.new("RGBA", (64, 64), T.BG_TOOLBAR)   # tray fallback
        return img

    def _setup_tray(self) -> None:
        """Create the system-tray icon. Degrades gracefully if unavailable."""
        if sys.platform != "win32":
            self._tray = None
            return
        try:
            import pystray

            # Downloads folded into the tray get one slot each. pystray evaluates
            # the text/visible callables when the menu is opened, so each slot
            # shows its download's live % and hides itself when unused — no need
            # to rebuild the menu as progress ticks.
            #
            # A factory binds i by CLOSURE, not via a `lambda ..., i=i` default:
            # pystray rejects an action whose co_argcount exceeds 2, and a default
            # argument counts toward that.
            def _slot(i):
                return pystray.MenuItem(
                    lambda item: self._tray_slot_text(i),
                    lambda icon, item: self._tray_slot_click(i),
                    visible=lambda item: self._tray_slot_visible(i),
                )
            slots = [_slot(i) for i in range(self.MAX_TRAY_DOWNLOADS)]
            menu = pystray.Menu(
                pystray.MenuItem("显示拾流下载器", self._tray_show, default=True),
                pystray.MenuItem("全部继续", self._tray_resume_all),
                pystray.MenuItem("全部暂停", self._tray_pause_all),
                pystray.Menu.SEPARATOR,
                # When no download is folded, every slot is invisible and this
                # collapses to just "退出" below the separator.
                *slots,
                pystray.MenuItem("退出", self._tray_exit),
            )
            self._tray = pystray.Icon(
                "magic_downloader", self._tray_image(), "拾流下载器", menu
            )
            self._tray_thread = threading.Thread(target=self._tray.run, daemon=True)
            self._tray_thread.start()
        except Exception:  # noqa: BLE001 — no tray → close will just exit
            self._tray = None

    # Single-instance control (called from the control-socket thread).
    def _request_quit(self) -> None:
        try:
            self.after(0, self._quit)
        except tk.TclError:
            pass

    def _request_show(self) -> None:
        try:
            self.after(0, self._restore_from_tray)
        except tk.TclError:
            pass

    # Tray callbacks run on the tray thread → marshal to the Tk main thread.
    def _tray_show(self, *_a) -> None:
        try:
            self.after(0, self._restore_from_tray)
        except tk.TclError:
            pass

    def _tray_exit(self, *_a) -> None:
        try:
            self.after(0, self._quit)
        except tk.TclError:
            pass

    def _tray_resume_all(self, *_a) -> None:
        self.after(0, lambda: [self.manager.retry_job(j.id) for j in list(self.manager.jobs)
                               if j.status in (DownloadStatus.PAUSED, DownloadStatus.QUEUED)])

    def _tray_pause_all(self, *_a) -> None:
        self.after(0, lambda: [self.manager.pause_job(j.id) for j in list(self.manager.jobs)])

    # ── fold a download's progress window into the tray (IDM-style) ──────────

    MAX_TRAY_DOWNLOADS = 8   # slots in the tray menu for folded downloads

    def _fold_download_to_tray(self, job_id: str) -> None:
        if job_id not in self._folded_downloads:
            self._folded_downloads.append(job_id)
        self._rebuild_folded_snapshot()
        self._refresh_tray_menu()

    def _restore_folded_download(self, job_id: str) -> None:
        self._folded_downloads = [j for j in self._folded_downloads if j != job_id]
        dlg = self._progress_dialogs.get(job_id)
        if dlg is not None and dlg.winfo_exists():
            dlg.restore()
        else:
            # The window was closed while folded — reopen it fresh.
            self._open_progress(job_id)
        self._rebuild_folded_snapshot()
        self._refresh_tray_menu()

    def _rebuild_folded_snapshot(self) -> None:
        """Build the tray menu's data as a plain (job_id, label) list, on the
        MAIN thread.

        The tray menu's text/visible/action callables run on the pystray thread
        (and are re-evaluated by update_menu() on the main thread when a fold
        rebuilds the menu). If they called manager.get_job() they'd take the
        manager lock there — and _persist holds that same lock across a disk
        write of jobs.json — so opening the tray menu, or folding a second
        download while others were active, stalled the whole GUI on that lock.
        Reading a pre-built plain list needs no lock and can't stall.
        """
        snap = []
        for jid in list(self._folded_downloads):
            job = self.manager.get_job(jid)   # main thread — lock here is fine
            if job is None:
                continue
            name = job.filename if len(job.filename) <= 34 else job.filename[:31] + "…"
            snap.append((jid, f"⬇ {name} — {job.progress:.0f}%"))
        self._folded_snapshot = snap          # atomic reassign; tray reads lock-free

    def _tray_slot_visible(self, i: int) -> bool:
        return i < len(self._folded_snapshot)

    def _tray_slot_text(self, i: int) -> str:
        snap = self._folded_snapshot          # one read; never touches the lock
        return snap[i][1] if i < len(snap) else ""

    def _tray_slot_click(self, i: int) -> None:
        snap = self._folded_snapshot
        if i < len(snap):
            jid = snap[i][0]
            self.after(0, lambda: self._restore_folded_download(jid))

    def _refresh_tray_menu(self) -> None:
        if self._tray is None:
            return
        try:
            self._tray.update_menu()
        except Exception:  # noqa: BLE001
            pass

    def _restore_from_tray(self) -> None:
        try:
            self.deiconify()
            self.state("normal")
            self.lift()
            self.focus_force()
        except tk.TclError:
            pass

    def _hide_to_tray(self) -> None:
        if self._tray is None:
            return
        try:
            self.withdraw()
        except tk.TclError:
            pass
        # (No tray balloon: it plays a Windows notification sound and pystray
        # has no silent option. The tray icon itself signals the app is alive.)

    def _on_minimize(self, event: tk.Event) -> None:
        # Hide to tray on the minimize button too, if the user opted in.
        if event.widget is not self:
            return
        if self._quitting or self._tray is None:
            return
        if not self.manager.settings.get("minimize_to_tray", False):
            return
        try:
            if self.state() == "iconic":
                self.after(10, self._hide_to_tray)
        except tk.TclError:
            pass

    def _on_close(self) -> None:
        # The window's X button: hide to tray unless disabled.
        if self._tray is not None and self.manager.settings.get("close_to_tray", True):
            self._hide_to_tray()
        else:
            self._quit()

    def _quit(self) -> None:
        if self._quitting:
            return
        if sys.platform == "darwin" and self._channels is not None:
            try:
                self._channels.stop()
            except Exception as exc:
                messagebox.showerror("视频号退出清理失败", str(exc), parent=self)
                return
        self._quitting = True
        try:
            if self._single_instance is not None:
                self._single_instance.close()
        except Exception:
            pass
        try:
            if self._tray is not None:
                self._tray.stop()
        except Exception:
            pass
        if self._browser:
            self._browser.stop()
        if sys.platform == "win32" and self._channels is not None:
            self._channels.stop()
        self.manager.shutdown()
        try:
            self.destroy()
        except tk.TclError:
            pass


def run_app() -> None:
    from magic_downloader.single_instance import SingleInstance


    # Single instance — "last one takes place": a new launch tells any running
    # instance to quit and takes over.
    si = SingleInstance()
    si.acquire(takeover=True)

    app = MagicDownloaderApp()
    app._single_instance = si
    si.start_listener(on_quit=app._request_quit, on_show=app._request_show)
    app.mainloop()
