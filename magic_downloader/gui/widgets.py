"""Reusable UI widgets (progress / segment bars)."""

from __future__ import annotations

import tkinter as tk

from magic_downloader.gui import theme as T
from magic_downloader.paths import migrated_path
from magic_downloader.models import DownloadJob, DownloadStatus, SegmentState


class ProgressBar(tk.Canvas):
    """Horizontal progress bar with percent text."""

    def __init__(self, master: tk.Misc, height: int = 18, **kwargs) -> None:
        super().__init__(
            master,
            height=height,
            bg=T.BG_LIST,
            highlightthickness=1,
            highlightbackground=T.BORDER,
            **kwargs,
        )
        self._value = 0.0
        self.bind("<Configure>", lambda e: self.redraw())

    def set_progress(self, percent: float, active: bool = False) -> None:
        self._value = max(0.0, min(100.0, percent))
        self._active = active
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w = max(1, self.winfo_width())
        h = max(1, self.winfo_height())
        fill = int(w * self._value / 100.0)
        color = T.GREEN if getattr(self, "_active", False) else T.GREEN_SEG_DONE
        if self._value >= 100:
            color = T.GREEN_SEG_DONE
        self.create_rectangle(0, 0, w, h, fill=T.BORDER, outline="")
        if fill > 0:
            self.create_rectangle(0, 0, fill, h, fill=color, outline="")
        label = f"{self._value:.1f}%"
        self.create_text(w // 2, h // 2, text=label, fill=T.FG, font=T.FONT_SMALL)


class SegmentBar(tk.Canvas):
    """multi-connection segment map (green blocks)."""

    def __init__(self, master: tk.Misc, height: int = 28, **kwargs) -> None:
        super().__init__(
            master,
            height=height,
            bg="#1a1a1a",
            highlightthickness=1,
            highlightbackground=T.BORDER,
            **kwargs,
        )
        self._job: DownloadJob | None = None
        self.bind("<Configure>", lambda e: self.redraw())

    def set_job(self, job: DownloadJob | None) -> None:
        self._job = job
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w = max(1, self.winfo_width())
        h = max(1, self.winfo_height())
        self.create_rectangle(0, 0, w, h, fill="#222222", outline="")

        job = self._job
        if not job or job.total_size <= 0:
            # Single stream progress fallback
            if job and job.downloaded > 0 and job.total_size > 0:
                fill = int(w * job.downloaded / job.total_size)
                self.create_rectangle(0, 2, fill, h - 2, fill=T.GREEN_SEG, outline="")
            elif job and job.status == DownloadStatus.COMPLETE:
                self.create_rectangle(0, 2, w, h - 2, fill=T.GREEN_SEG_DONE, outline="")
            else:
                self.create_text(
                    w // 2, h // 2, text="暂无分段数据", fill="#888", font=T.FONT_SMALL
                )
            return

        segments: list[SegmentState] = job.segments or []
        if not segments:
            # Approximate single bar
            pct = job.downloaded / job.total_size
            fill = max(1, int(w * pct))
            color = T.GREEN_SEG if job.status == DownloadStatus.DOWNLOADING else T.GREEN_SEG_DONE
            self.create_rectangle(0, 2, fill, h - 2, fill=color, outline="")
            return

        total = job.total_size
        for seg in segments:
            x0 = int(w * seg.start / total)
            x1 = int(w * (seg.end + 1) / total)
            # background for full segment range
            self.create_rectangle(x0, 2, max(x0 + 1, x1), h - 2, fill="#333333", outline="")
            if seg.downloaded > 0:
                done_end = seg.start + seg.downloaded
                xd = int(w * done_end / total)
                color = T.GREEN_SEG
                if seg.remaining <= 0:
                    color = T.GREEN_SEG_DONE
                elif job.status == DownloadStatus.PAUSED:
                    color = T.ORANGE
                self.create_rectangle(x0, 2, max(x0 + 1, xd), h - 2, fill=color, outline="")

        # divider lines between segments
        for seg in segments[1:]:
            x = int(w * seg.start / total)
            self.create_line(x, 0, x, h, fill="#111111")



def rounded_rect(canvas, x1, y1, x2, y2, radius=6, **kwargs):
    radius = min(radius, (x2-x1)/2, (y2-y1)/2)
    return canvas.create_polygon(
        x1+radius,y1, x2-radius,y1, x2,y1, x2,y1+radius,
        x2,y2-radius, x2,y2, x2-radius,y2, x1+radius,y2,
        x1,y2, x1,y2-radius, x1,y1+radius, x1,y1,
        smooth=True, splinesteps=16, **kwargs)


def draw_icon(canvas, x, y, name, color, size=18):
    """One stroke weight for navigation, task types and action controls."""
    k = size / 20
    def line(*pts):
        canvas.create_line(*[v*k + (x if i%2 == 0 else y) for i, v in enumerate(pts)],
                           fill=color, width=1.5, capstyle=tk.ROUND, joinstyle=tk.ROUND)
    def box(a,b,c,d):
        canvas.create_rectangle(x+a*k,y+b*k,x+c*k,y+d*k,outline=color,width=1.5)
    def oval(a,b,c,d):
        canvas.create_oval(x+a*k,y+b*k,x+c*k,y+d*k,outline=color,width=1.5)
    if name in ("all", "General", "folder"):
        line(2,6,2,17,18,17,18,5,10,5,8,3,2,3,2,6)
    elif name in ("downloading", "add"):
        if name == "add": line(10,3,10,17); line(3,10,17,10)
        else: line(10,2,10,16); line(5,11,10,16,15,11); line(15,18,18,18)
    elif name == "queued": oval(2,2,18,18); line(10,5,10,10,14,12)
    elif name in ("paused", "pause"): line(6,3,6,17); line(14,3,14,17)
    elif name == "complete": oval(2,2,18,18); line(6,10,9,13,14,7)
    elif name == "failed": oval(2,2,18,18); line(5,15,15,5)
    elif name == "Video": box(2,4,14,16); line(14,7,18,4,18,16,14,13)
    elif name == "Music": line(8,15,8,4,17,2,17,13); oval(2,13,8,18); oval(11,11,17,16)
    elif name in ("Documents", "Compressed"):
        line(4,2,12,2,17,7,17,18,4,18,4,2); line(12,2,12,7,17,7)
        if name == "Compressed": line(9,4,9,13); box(8,13,10,15)
    elif name == "play": line(6,3,6,17,17,10,6,3)
    elif name == "stop": box(4,4,16,16)
    elif name == "trash": line(3,5,17,5); line(7,5,7,2,13,2,13,5); line(5,5,6,18,14,18,15,5); line(9,8,9,15); line(12,8,12,15)
    elif name == "open": line(9,4,3,4,3,17,16,17,16,11); line(10,2,18,2,18,10); line(18,2,8,12)
    elif name == "settings": oval(6,6,14,14); oval(2,2,18,18); line(10,0,10,3); line(10,17,10,20); line(0,10,3,10); line(17,10,20,10)
    elif name == "chevron": line(5,7,10,12,15,7)
    elif name == "sort": line(6,3,6,17,3,14); line(6,17,9,14); line(14,17,14,3,11,6); line(14,3,17,6)
    elif name == "list":
        for z in (4,10,16): line(7,z,18,z); oval(2,z-1,3,z)
    else:
        for z in (4,10,16):
            canvas.create_oval(x+z*k-1,y+9*k-1,x+z*k+1,y+9*k+1,fill=color,outline="")


class ToolbarButton(tk.Frame):
    """Compact horizontal action with a shared linear icon."""
    def __init__(self, master, icon, text, command, bg=T.BG_TOOLBAR,
                 primary=False, outlined=False, compact=False, **kwargs):
        super().__init__(master, bg=T.ACCENT if primary else bg,
                         highlightthickness=1 if outlined else 0,
                         highlightbackground=T.BORDER, **kwargs)
        self._command, self._icon = command, icon
        self._bg = T.ACCENT if primary else bg
        self._hover = "#266bea" if primary else T.ACCENT_HOVER
        self._primary, self._enabled = primary, True
        self._outlined, self._focused = outlined, False
        self._surface = master.cget("bg")
        self.configure(bg=self._surface, highlightthickness=0)
        self._back = tk.Canvas(self, bg=self._surface, bd=0, highlightthickness=0)
        self._back.place(x=0,y=0,relwidth=1,relheight=1)
        self._back.bind("<Button-1>", self._click)
        self._back.bind("<Enter>", self._enter)
        self._back.bind("<Leave>", self._leave)
        self.bind("<Configure>", lambda e: self._paint(self._bg))
        self.icon_lbl = tk.Canvas(self, width=18, height=18, bg=self._bg,
                                  bd=0, highlightthickness=0)
        self.icon_lbl.pack(side=tk.LEFT, padx=(9, 4 if text else 9), pady=(3, 4) if compact else 6)
        self.text_lbl = tk.Label(self, text=text, bg=self._bg,
                                 fg="white" if primary else T.FG, font=T.FONT_BUTTON)
        if text: self.text_lbl.pack(side=tk.LEFT, padx=(0,11))
        for w in (self, self.icon_lbl, self.text_lbl):
            w.configure(cursor="hand2")
            w.bind("<Button-1>", self._click)
            w.bind("<Enter>", self._enter)
            w.bind("<Leave>", self._leave)
        self.bind("<Return>", self._click)
        self.bind("<space>", self._click)
        self.configure(takefocus=1)
        self.bind("<FocusIn>", lambda e: self._focus(True))
        self.bind("<FocusOut>", lambda e: self._focus(False))
        self._draw()

    def _paint(self, fill):
        self._back.delete("all")
        rounded_rect(self._back, 1,1,max(2,self.winfo_width()-1),max(2,self.winfo_height()-1),
                     fill=fill, outline=T.ACCENT if self._focused else T.BORDER if self._outlined else fill)
        for w in (self.icon_lbl,self.text_lbl): w.configure(bg=fill)

    def _focus(self, focused):
        self._focused = focused
        self._paint(self._bg)

    def _draw(self):
        self.icon_lbl.delete("all")
        color = ("white" if self._primary else T.FG) if self._enabled else T.FG_ON_DARK_DISABLED
        draw_icon(self.icon_lbl, 1, 1, self._icon, color, 16)
        self.text_lbl.configure(fg=color)

    def set_enabled(self, enabled):
        if self._enabled == bool(enabled): return
        self._enabled = bool(enabled)
        self._draw()
        self._paint(self._bg)
        for w in (self, self.icon_lbl, self.text_lbl):
            w.configure(cursor="hand2" if enabled else "arrow")

    def _click(self, event=None):
        if self._enabled and self._command: self._command()

    def _enter(self, event=None):
        if self._enabled:
            self._paint(self._hover)

    def _leave(self, event=None):
        self._paint(self._bg)


class Sidebar(tk.Canvas):
    """Navigation with right-aligned counts; preserves the existing filter API."""
    def __init__(self, master, on_select):
        super().__init__(master, bg=T.BG_SIDEBAR, bd=0, highlightthickness=0, takefocus=1)
        self.items, self.counts, self._selected, self._boxes = [], [], None, []
        self.on_select = on_select
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-1>", self._click)
        self.bind("<Up>", lambda e: self._step(-1))
        self.bind("<Down>", lambda e: self._step(1))
        self.bind("<MouseWheel>", lambda e: self.yview_scroll(-int(e.delta), "units"))

    def set_items(self, items, counts):
        self.items, self.counts = items, counts
        self.redraw()

    def curselection(self):
        return () if self._selected is None else (self._selected,)

    def selection_clear(self, *args): self._selected = None

    def selection_set(self, index):
        self._selected = int(index)
        self.redraw()

    def nearest(self, y):
        y = self.canvasy(y)
        return next((i for i,(top,bottom) in enumerate(self._boxes) if top <= y < bottom), -1)

    def _click(self, event):
        i = self.nearest(event.y)
        if i >= 0 and self.items[i][1]:
            self.focus_set(); self.selection_set(i); self.on_select()

    def _step(self, step):
        i = (self._selected or 0) + step
        while 0 <= i < len(self.items):
            if self.items[i][1]:
                self.selection_set(i); self.on_select(); return
            i += step

    def redraw(self):
        self.delete("all")
        width = max(200, self.winfo_width())
        self.create_text(10, 14, text="下载任务", fill=T.FG_MUTED, font=T.FONT_SMALL, anchor="w")
        y = 28
        self._boxes = []
        for i, ((label,key),count) in enumerate(zip(self.items,self.counts)):
            height = 32 if not key else 26
            self._boxes.append((y,y+height))
            if not key:
                self.create_text(10,y+22,text=label,fill=T.FG_MUTED,font=T.FONT_SMALL,anchor="w")
            else:
                color = T.SELECT_FG if i == self._selected else T.FG
                if i == self._selected:
                    rounded_rect(self,1,y+1,width-1,y+height-1,fill=T.SELECT,outline="")
                draw_icon(self, 10,y+4,key[4:] if key.startswith("cat:") else key,color)
                self.create_text(43,y+13,text=label,fill=color if i == self._selected else T.FG_BRAND,
                                 font=T.FONT_NAV_SELECTED if i == self._selected else T.FONT_NAV,anchor="w")
                self.create_text(width-12,y+13,text=str(count),fill=color if i == self._selected else T.FG_SUBTLE,
                                  font=T.FONT_AUX,anchor="e")
            y += height
        self.configure(scrollregion=(0,0,width,y))


class TaskRows:
    """Paint the visible native Treeview rows; sorting/selection stay in Treeview."""
    def __init__(self, tree, get_job, context_menu):
        from tkinter import font
        self.tree, self.get_job, self.context_menu = tree, get_job, context_menu
        self.rows, self._pending = {}, False
        self.font = font.Font(root=tree, font=T.FONT_TASK)
        self.small = font.Font(root=tree, font=T.FONT_SMALL)
        for event in ("<Configure>", "<<TreeviewSelect>>", "<KeyRelease>"):
            tree.bind(event, lambda e: self.schedule(), add="+")

    def schedule(self):
        if self._pending: return
        self._pending = True
        self.tree.after_idle(self.redraw)

    def _text(self, canvas, text, x, y, width, muted=False):
        font = self.small if muted else self.font
        text = str(text).replace("\n", " ")
        if font.measure(text) > width:
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo+hi+1)//2
                if font.measure(text[:mid]+"…") <= width: lo = mid
                else: hi = mid-1
            text = text[:lo]+"…"
        canvas.create_text(x,y,text=text,font=font,fill=T.FG_SUBTLE if muted else T.FG_BRAND,anchor="w")

    def _forward(self, canvas, event, sequence):
        self.tree.event_generate(sequence, x=event.x+canvas.winfo_x(),
            y=event.y+canvas.winfo_y(), state=event.state)
        self.tree.focus_set()
        self.schedule()

    def _click(self, canvas, event, iid):
        if event.x < 34:
            if iid in self.tree.selection(): self.tree.selection_remove(iid)
            else: self.tree.selection_add(iid)
            self.tree.focus(iid); self.tree.focus_set(); self.schedule()
        elif event.x > self.tree.winfo_width()-32:
            if iid not in self.tree.selection(): self.tree.selection_set(iid)
            from types import SimpleNamespace
            self.context_menu(SimpleNamespace(x=event.x,y=event.y+canvas.winfo_y(),
                x_root=event.x_root,y_root=event.y_root))
        else:
            self._forward(canvas,event,"<Button-1>")

    def redraw(self):
        import time
        from magic_downloader.gui.app import COLUMNS, STATUS_CN
        self._pending = False
        if not self.tree.winfo_exists(): return
        selected = set(self.tree.selection())
        visible = set()
        width, height = self.tree.winfo_width(), self.tree.winfo_height()
        for iid in self.tree.get_children():
            bounds = self.tree.bbox(iid)
            if not bounds: continue
            _, y, _, h = bounds
            if y < 0 or y >= height: continue
            visible_height = min(h,height-y)
            job = self.get_job(iid)
            if job is None: continue
            visible.add(iid)
            if iid not in self.rows:
                c = tk.Canvas(self.tree, highlightthickness=0, bd=0)
                c.bind("<Button-1>", lambda e,c=c,i=iid: self._click(c,e,i))
                c.bind("<Double-Button-1>", lambda e: self.tree.winfo_toplevel()._on_double_click())
                c.bind("<Button-3>", lambda e,c=c: self._forward(c,e,"<Button-3>"))
                c.bind("<Button-2>", lambda e,c=c: self._forward(c,e,"<Button-2>"))
                c.bind("<MouseWheel>", lambda e: self.tree.yview_scroll(-int(e.delta),"units"))
                self.rows[iid] = c
            c = self.rows[iid]
            c.place(x=0,y=y,width=width,height=visible_height)
            bg = T.SELECT if iid in selected else T.BG_LIST
            cols = list(self.tree.cget("displaycolumns"))
            if cols == ["#all"]: cols = list(COLUMNS)
            cells = [(key,self.tree.bbox(iid,key)) for key in cols]
            values = self.tree.item(iid,"values")
            signature = (tuple(values),tuple((k,tuple(b)) for k,b in cells), iid in selected,width,visible_height,int(time.time()//60))
            # Keep idle rows intact so refresh ticks do not flash the list.
            if getattr(c,"_signature",None) == signature: continue
            c._signature = signature
            c.configure(bg=bg); c.delete("all")
            c.create_line(0,h-1,width,h-1,fill=T.BORDER)
            c.create_rectangle(10,h//2-7,24,h//2+7,outline=T.ACCENT if iid in selected else "#b7c1d1",
                                fill=T.ACCENT if iid in selected else bg,width=1)
            if iid in selected:
                c.create_line(13,h//2,17,h//2+4,22,h//2-3,fill="white",width=1.5)
            for key,b in cells:
                if not b: continue
                x,_,w,_ = b
                if x+w <= 34 or x >= width: continue
                x += 8; w -= 16
                if key == "filename":
                    color = T.ACCENT
                    c.create_rectangle(x+2,8,x+23,34,fill="#6689da",outline="")
                    c.create_polygon(x+16,8,x+23,15,x+16,15,fill="#b4c8f0",outline="")
                    if job.category == "Video":
                        c.create_polygon(x+9,17,x+9,27,x+17,22,fill="white",outline="")
                    elif job.category == "Music":
                        c.create_text(x+12,26,text="♪",fill="white",font=T.FONT_UI)
                    else:
                        c.create_line(x+7,24,x+18,24,fill="white"); c.create_line(x+7,29,x+18,29,fill="white")
                    self._text(c,job.filename,x+36,10,w-38)
                    self._text(c,str(migrated_path(job.save_path).parent),x+36,28,w-38,True)
                elif key == "status":
                    color = T.STATUS_COLORS.get(job.status.value,T.GRAY)
                    label = STATUS_CN.get(job.status.value,job.status.value)
                    bw = min(w,self.small.measure(label)+32)
                    fill = {"Complete":"#e5f8e9","Failed":"#ffeded","Paused":"#fff4dc"}.get(job.status.value,"#edf3ff")
                    rounded_rect(c,x,h//2-10,x+bw,h//2+10,fill=fill,outline="")
                    draw_icon(c,x+5,h//2-8,{"Complete":"complete","Downloading":"downloading",
                              "Connecting":"queued","Paused":"pause","Failed":"failed"}.get(job.status.value,"queued"),color,16)
                    c.create_text(x+27,h//2,text=label,font=self.small,fill=color,anchor="w")
                elif key == "progress":
                    pct = job.progress
                    bar_w = max(12,w-46)
                    c.create_line(x,h//2,x+bar_w,h//2,fill=T.BORDER,width=10,capstyle=tk.ROUND)
                    if pct > 0:
                        c.create_line(x,h//2,x+bar_w*min(100,pct)/100,h//2,fill=T.ACCENT,width=10,capstyle=tk.ROUND)
                    known = job.total_size > 0 or job.status == DownloadStatus.COMPLETE or bool(job.media_meta.get("seg_total"))
                    self._text(c,f"{pct:.0f}%" if known else "—",x+bar_w+12,h//2,44,True)
                elif key == "date":
                    stamp = job.created_at
                    delta = max(0,time.time()-stamp) if stamp else 0
                    label = ("刚刚" if delta<60 else f"{int(delta//60)}分钟前" if delta<3600
                             else f"{int(delta//3600)}小时前" if delta<86400
                             else time.strftime("%m-%d %H:%M",time.localtime(stamp))) if stamp else "—"
                    self._text(c,label,x,h//2,w-16,True)
                else:
                    self._text(c,values[COLUMNS.index(key)],x,h//2,w,True)
            # Cover the fixed checkbox gutter when horizontally scrolled.
            c.create_rectangle(0,0,33,h-2,fill=bg,outline="")
            c.create_rectangle(10,h//2-7,24,h//2+7,outline=T.ACCENT if iid in selected else "#b7c1d1",
                               fill=T.ACCENT if iid in selected else bg)
            if iid in selected: c.create_line(13,h//2,17,h//2+4,22,h//2-3,fill="white",width=1.5)
            draw_icon(c,width-24,h//2-8,"more",T.FG,16)
        for iid in list(self.rows):
            if iid not in visible: self.rows.pop(iid).destroy()
