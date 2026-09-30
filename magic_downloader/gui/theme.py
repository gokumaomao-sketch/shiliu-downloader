"""拾流下载器 V2：浅色 macOS 工具配色。"""
BG = "#ffffff"
BG_DARK = "#f1f4f8"
BG_TOOLBAR = "#ffffff"
BG_SIDEBAR = "#f1f4f8"
BG_LIST = "#ffffff"
BG_DETAIL = "#ffffff"
BG_STATUS = "#fafbfd"
STRIPE = "#ffffff"
FG = "#172640"
FG_MUTED = "#8994a5"
FG_SUBTLE = "#a6afbb"
FG_BRAND = "#344760"
FG_ON_DARK = FG
FG_ON_DARK_DISABLED = "#acb4c2"
FG_ON_DARK_MUTED = FG_MUTED
TOOLBAR_SEP = "#e8edf4"
SPEED_BADGE = "#24864b"
AMBER = "#a87616"
TOAST_BG = "#3478f6"
ACCENT = "#3478f6"
ACCENT_HOVER = "#edf3ff"
GREEN = "#219b58"
GREEN_DONE = "#25894a"
GREEN_SEG = ACCENT
GREEN_SEG_DONE = ACCENT
ORANGE = "#b47b12"
RED = "#ce4242"
BLUE = ACCENT
GRAY = "#8490a4"
BORDER = "#e5eaf1"
SELECT = "#e2edff"
SELECT_FG = "#246af0"
# Native macOS faces select real weights instead of synthetic Tk bold.
import sys
import tkinter.font as tkfont

REGULAR_FACE = "PingFangSC-Regular" if sys.platform == "darwin" else "Segoe UI"
MEDIUM_FACE = "PingFangSC-Medium" if sys.platform == "darwin" else "Segoe UI"
SEMIBOLD_FACE = "PingFangSC-Semibold" if sys.platform == "darwin" else "Segoe UI Semibold"
FONT_TITLE = (MEDIUM_FACE, 14)
FONT_BRAND = (MEDIUM_FACE, 12)
FONT_BODY = (REGULAR_FACE, 11)
FONT_MEDIUM = (MEDIUM_FACE, 11)
FONT_NAV = (REGULAR_FACE, 10)
FONT_NAV_SELECTED = (MEDIUM_FACE, 10)
FONT_TASK = (REGULAR_FACE, 10)
FONT_BUTTON = (MEDIUM_FACE, 10)
FONT_SEARCH = (REGULAR_FACE, 10)
FONT_HEADER = (MEDIUM_FACE, 9)
FONT_AUX = (REGULAR_FACE, 9)
FONT_STATUS = FONT_AUX
FONT_SYMBOL = (REGULAR_FACE, 22)
# Existing reusable widgets share the same semantic tokens.
FONT_UI = FONT_BODY
FONT_SMALL = FONT_AUX
FONT_MONO = FONT_AUX
FONT_TOOLBAR = FONT_BUTTON


def apply_fonts(root):
    """Set typography once for the app and its native ttk/Tk dialog children."""
    if sys.platform == "darwin":
        # Previous macOS scale was 0.875; another 10% tightens the whole app.
        root.tk.call("tk", "scaling", float(root.tk.call("tk", "scaling")) * 0.7875)
    roles = {
        "TkDefaultFont": FONT_BODY, "TkTextFont": FONT_BODY,
        "TkMenuFont": FONT_BODY, "TkHeadingFont": FONT_HEADER,
        "TkCaptionFont": FONT_TITLE, "TkSmallCaptionFont": FONT_AUX,
        "TkTooltipFont": FONT_AUX, "TkIconFont": FONT_BODY,
    }
    for name, (face, size) in roles.items():
        tkfont.nametofont(name, root=root).configure(family=face, size=size, weight="normal")


    # 按需导出实际窗口的字体，用于视觉验收；默认不写文件。
    import os
    audit_path = os.environ.get("SHILIU_FONT_AUDIT_PATH")
    if audit_path:
        root.after(1800, lambda: export_font_audit(root, audit_path))


def export_font_audit(root, path):
    import json
    import tkinter as tk
    from tkinter import ttk
    from pathlib import Path

    def resolved(requested):
        font = tkfont.Font(root=root, font=requested)
        return {"requested": str(requested), "actual": font.actual(), "metrics": font.metrics()}

    style = ttk.Style(root)
    result = {"scaling": float(root.tk.call("tk", "scaling")),
              "named_fonts": {}, "styles": {}, "widgets": [], "canvas_fonts": []}
    for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont"):
        result["named_fonts"][name] = resolved(name)
    for name in ("Treeview", "Treeview.Heading", "TButton", "TLabel", "TEntry"):
        result["styles"][name] = {**resolved(style.lookup(name, "font") or "TkDefaultFont"),
                                  "foreground": str(style.lookup(name, "foreground")),
                                  "padding": str(style.lookup(name, "padding")),
                                  "rowheight": str(style.lookup(name, "rowheight"))}
    def inspect(widget):
        try:
            font = widget.cget("font")
        except tk.TclError:
            font = None
        if font:
            result["widgets"].append({"path": str(widget), "class": widget.winfo_class(),
                "text": str(widget.cget("text")) if "text" in widget.keys() else "",
                "foreground": str(widget.cget("fg")) if "fg" in widget.keys() else "",
                "height": widget.winfo_height(), "parent_height": widget.master.winfo_height(), **resolved(font)})
        if isinstance(widget, tk.Canvas):
            seen = set()
            for item in widget.find_all():
                if widget.type(item) != "text":
                    continue
                font = widget.itemcget(item, "font")
                fill = widget.itemcget(item, "fill")
                if (font, fill) in seen:
                    continue
                seen.add((font, fill))
                result["canvas_fonts"].append({"path": str(widget),
                    "text": widget.itemcget(item, "text"), "foreground": fill, **resolved(font)})
        for child in widget.winfo_children():
            inspect(child)
    inspect(root)
    if hasattr(root, "task_rows"):
        result["task_fonts"] = {"filename": resolved(root.task_rows.font),
                                "auxiliary": resolved(root.task_rows.small)}
    result["geometry"] = root.winfo_geometry()
    Path(path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


STATUS_COLORS = {
    "Queued": GRAY, "Connecting": BLUE, "Downloading": BLUE,
    "Processing": BLUE, "Paused": ORANGE, "Complete": GREEN_DONE,
    "Failed": RED, "Cancelled": GRAY,
}
