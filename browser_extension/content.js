/**
 * 拾流下载器 — on-page controls (the visible download-button experience).
 *
 *  • A "⬇ Download" button appears on every <video> player (top-right).
 *  • A floating action button (bottom-right) shows how many videos/streams the
 *    extension sniffed on this page.
 *  • Clicking either opens a panel listing the real media (HLS/DASH/MP4/…) with
 *    a Download button per item — sent straight to the desktop app.
 */

(function () {
  if (window.__magicDownloaderInjected) return;
  window.__magicDownloaderInjected = true;

  const ROOT_ID = "magic-downloader-root";
  const MIN_VIDEO_W = 160;
  const MIN_VIDEO_H = 90;

  let appOnline = false;
  let appHasFfmpeg = true;
  let cfg = { enabled: true, showVideoButton: true, showFloatingButton: true };
  const videoOverlays = new Map(); // videoEl -> button
  // Drag offset (relative to each video's default top-right anchor) so the user
  // can reposition the on-video "Download" button; applies to all videos.
  let videoBtnOffset = { dx: 0, dy: 0 };
  let overlayDragging = false;
  let overlaySuppressClick = false;
  const VIDEO_BTN_OFFSET_KEY = "md_vbtn_offset";

  // ── messaging ─────────────────────────────────────────────────────────
  // Firefox = promise-based `browser`; Chrome/Edge MV3 = promise-based `chrome`.
  const B = (typeof browser !== "undefined" && browser) || chrome;
  function msg(payload) {
    return new Promise((resolve) => {
      try {
        const p = B.runtime.sendMessage(payload);
        if (p && typeof p.then === "function") {
          p.then((res) => resolve(res || { ok: false })).catch((error) => { resolve({ ok: false }); });
        } else {
          resolve({ ok: false });
        }
      } catch (_) {
        resolve({ ok: false });
      }
    });
  }
  const ping = () => msg({ type: "ping" });
  const getConfig = () => msg({ type: "getConfig" });
  const getMedia = () => msg({ type: "getMedia" });
  const previewDestination = (item) => msg({ type: "previewDestination", item });
  const downloadMedia = (item, sel) => msg({ type: "downloadMedia", item, sel });
  const sendUrl = (url, opts) => msg({ type: "add", url, opts });

  // Tell the background this page is showing a video → enables the yt-dlp
  // "download this page's video" path (the only thing that works on such sites).
  let lastReported = "";
  function reportPageVideo() {
    const title = document.querySelector('meta[property="og:title"]')?.content?.trim()
      || document.querySelector("h1")?.textContent?.trim() || document.title;
    const key = location.href + "|" + title;
    if (key === lastReported) return;
    lastReported = key;
    msg({ type: "reportPageVideo", pageUrl: location.href, title });
  }

  function guessName(url) {
    try {
      const u = new URL(url, location.href);
      const last = u.pathname.split("/").filter(Boolean).pop() || "download";
      return decodeURIComponent(last.split("?")[0]) || "download";
    } catch {
      return "download";
    }
  }

  // ── shared panel ──────────────────────────────────────────────────────
  let panel = null;
  let focusedItems = null;
  let panelAnchor = null;
  let pageSnapshot = location.href;
  let pageUrl = location.href;
  const isInstagram = /(^|\.)instagram\.com$/.test(location.hostname);
  const PANEL_POS_KEY = "md_panel_pos";
  let panelPos = null;   // {left, top} once the user has dragged the panel

  function buildPanel(root) {
    panel = document.createElement("div");
    panel.id = "md-panel";
    panel.innerHTML = `
      <h3 title="拖动标题可移动，拖动右下角可调整大小">拾流下载器 v${B.runtime.getManifest().version} <button class="md-close" title="关闭">×</button></h3>
      <div id="md-status" class="md-status">正在连接软件…</div>
      <div id="md-list"></div>
    `;
    root.appendChild(panel);
    panel.querySelector(".md-close").addEventListener("click", () => closePanel());
    panel.addEventListener("click", (e) => e.stopPropagation());
    // Drag the panel by its title bar; the × must not start a drag.
    makePanelDraggable(panel.querySelector("h3"));
    panel.querySelector(".md-close").addEventListener("pointerdown", (e) => e.stopPropagation());
    restorePanelPos();
  }

  function setPanelPos(left, top) {
    const w = panel.offsetWidth || 340;
    const h = panel.offsetHeight || 200;
    const l = Math.max(2, Math.min(window.innerWidth - w - 2, left));
    const t = Math.max(2, Math.min(window.innerHeight - h - 2, top));
    panel.style.setProperty("left", l + "px", "important");
    panel.style.setProperty("top", t + "px", "important");
    panel.style.setProperty("right", "auto", "important");
    panel.style.setProperty("bottom", "auto", "important");
  }

  function makePanelDraggable(handle) {
    let ox = 0;
    let oy = 0;
    attachDrag(handle, {
      onStart: () => {
        const pr = panel.getBoundingClientRect();
        ox = pr.left;
        oy = pr.top;
        setPanelPos(pr.left, pr.top); // switch from right/bottom to left/top
      },
      onMove: (dx, dy) => setPanelPos(ox + dx, oy + dy),
      onEnd: (moved) => {
        if (!moved) return;
        const pr = panel.getBoundingClientRect();
        panelPos = { left: pr.left, top: pr.top };
        try {
          B.storage.local.set({ [PANEL_POS_KEY]: panelPos });
        } catch (_) {
          /* ignore */
        }
      },
    });
  }

  function restorePanelPos() {
    try {
      B.storage.local.get(PANEL_POS_KEY).then((r) => {
        const p = r && r[PANEL_POS_KEY];
        if (p && typeof p.left === "number" && !panel.classList.contains("md-open")) panelPos = p;
      });
    } catch (_) {
      /* storage unavailable */
    }
  }

  function openPanel(items = null, anchor = null) {
    focusedItems = items;
    panelAnchor = anchor;
    if (anchor) panelPos = null;
    if (!panel) return;
    panel.classList.add("md-open");   // show first so its size is measurable
    positionPanel();
    refreshStatus();
    renderList();
  }
  function closePanel() {
    if (panel) panel.classList.remove("md-open");
  }

  function resetForPageChange() {
    const current = location.href;
    if (current === pageSnapshot) return;
    pageSnapshot = current;
    focusedItems = null;
    panelAnchor = null;
    panelPos = null;
    closePanel();
    const card = panel?.querySelector("#md-download");
    if (card) {
      card.style.display = "none";
      card.textContent = "";
    }
    if (downloadTimer) {
      clearInterval(downloadTimer);
      downloadTimer = null;
    }
    fab?.classList.remove("md-downloading");
    fab?.querySelector(".md-fab-progress")?.style.setProperty("width", "0");
  }
  function togglePanel(anchor = null) {
    if (!panel) return;
    if (panel.classList.contains("md-open")) closePanel();
    else openPanel(null, anchor);
  }

  async function refreshStatus() {
    const el = panel && panel.querySelector("#md-status");
    if (!el) return;
    const res = await ping();
    appOnline = !!res.ok;
    appHasFfmpeg = res.ffmpeg !== false;
    if (res.ok) {
      el.textContent = `已连接 · 端口 ${res.port || "7374"}${
        res.ffmpeg === false ? " · ⚠ 未安装 ffmpeg，高清音视频可能无法合并" : ""
      }`;
      el.className = "md-status ok";
    } else {
      el.textContent = "桌面软件未运行，请先启动 拾流下载器。";
      el.className = "md-status bad";
    }
    updateFab();
  }

  function mediaLabel(item) {
    if (item.kind === "page") return "当前页面的视频";
    if (item.kind === "hls") return "HLS 视频流";
    if (item.kind === "dash") return "DASH 视频流";
    const size = item.size ? ` · ${humanSize(item.size)}` : "";
    return `${item.ext ? item.ext.toUpperCase() : "文件"}${size}`;
  }

  function humanSize(n) {
    if (!n) return "";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < u.length - 1) {
      v /= 1024;
      i++;
    }
    return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
  }

  async function renderList() {
    const listEl = panel && panel.querySelector("#md-list");
    if (!listEl) return;
    const res = await getMedia();
    let items = (res && res.items) || [];
    if (isInstagram && !focusedItems) items = /\/(?:reels?|p)\/[\w-]+/.test(location.pathname) ? items.filter(i=>i.kind==='page') : [];
    const primary = focusedItems?.[0] || items.find((item) => item.kind === "page") || items[0];
    const others = items.filter((item) => item.url !== primary?.url);
    listEl.innerHTML = "";
    if (!primary) {
      const empty = document.createElement("div");
      empty.className = "md-empty";
      empty.textContent =
        isInstagram ? "请点击具体视频旁的下载按钮；不混合首页其他帖子的资源。" : "尚未识别到视频。请先播放，再重新打开此面板。";
      listEl.appendChild(empty);
      return;
    }
    const destination = document.createElement("div");
    destination.className = "md-destination";
    destination.textContent = "正在读取保存位置…";
    listEl.appendChild(destination);
    previewDestination(primary).then((result) => {
      if (!destination.isConnected) return;
      destination.textContent = result?.ok
        ? `推荐视频保存到：${result.folder}${result.rule ? ` · 站点归档：${result.rule}` : ` · ${result.category || '默认目录'}`}`
        : "保存位置暂不可用，请检查桌面软件。";
      destination.title = destination.textContent;
    });
    if (primary.ambiguous) {
      const warning = document.createElement("div");
      warning.className = "md-ambiguous";
      warning.textContent = "此页面有多个播放器，当前播放器未提供独立链接；网页推荐可能不是所点视频。";
      listEl.appendChild(warning);
    }
    listEl.appendChild(renderItem(primary));
    if (others.length) {
      const details = document.createElement("details");
      details.innerHTML = `<summary>其他检测到的资源（${others.length}）</summary>`;
      details.addEventListener("toggle", () => {
        if (!details.open || details.dataset.loaded) return;
        details.dataset.loaded = "1";
        for (const item of others) details.appendChild(renderItem(item));
      });
      listEl.appendChild(details);
    }
    requestAnimationFrame(() => positionPanel());
  }

  function renderItem(item) {
    const kind = item.kind === "file" ? "file" : item.kind;
    const name = item.title || item.name || guessName(item.url);
    const isPage = item.kind === "page";
    const isStream = item.kind === "hls" || item.kind === "dash";
    const hasPicker = isPage || isStream;   // page/HLS/DASH → list every quality

    const block = document.createElement("div");
    block.className = "md-item-block";

    const head = document.createElement("div");
    head.className = "md-item";
    head.innerHTML = `
      <span class="md-kind ${kind}">${({page:'网页',file:'文件',hls:'视频流',dash:'视频流'})[item.kind] || '文件'}</span>
      <div class="md-meta">
        <div class="md-name" title="${escapeHtml(item.url)}">${escapeHtml(name)}</div>
        <div class="md-sub">${escapeHtml(mediaLabel(item))}</div>
      </div>
      ${hasPicker ? "" : '<button class="md-get" type="button">下载</button>'}
    `;
    block.appendChild(head);

    // Plain file: one Download button, no quality list.
    if (!hasPicker) {
      head.querySelector(".md-get").addEventListener(
        "click", (e) => doDownload(item, {}, e.currentTarget, name));
      return block;
    }

    // Page / stream: probe the app for every quality and list them, exactly like
    // the toolbar popup — so the on-video button no longer punts to the popup.
    const list = document.createElement("div");
    list.className = "md-formats";
    list.innerHTML = '<div class="md-floading">正在读取画质…</div>';
    block.appendChild(list);

    let probeFinished = false;
    if (isPage && /(^|\.)youtube\.com$|(^|\.)youtu\.be$/.test(location.hostname)) {
      msg({type: "probeBrowserYouTube", url: item.url}).then((quick) => {
        if (probeFinished || !list.isConnected || !quick?.formats?.length) return;
        list.innerHTML = '<div class="md-floading">浏览器画质已识别，继续读取更多画质…</div>';
        renderFormats(item, name, head, list, quick);
      });
    }
    msg({
      type: "probeMedia",
      url: item.url,
      opts: { media_type: isPage ? "page" : item.kind, pageUrl: item.pageUrl || location.href },
    }).then((res) => {
      probeFinished = true;
      list.innerHTML = "";
      if (res?.browser_diagnostic) {
        const diagnostic = document.createElement("div");
        diagnostic.className = "md-floading";
        diagnostic.textContent = res.browser_diagnostic;
        list.appendChild(diagnostic);
      }
      let formats = [];
      if (isPage) {
        formats = (res && res.formats) || [];
        const sub = head.querySelector(".md-sub");
        if (res && res.extractor && sub) sub.textContent = `${res.extractor} · ${formats.length} 种画质`;
      } else {
        formats = ((res && res.variants) || [])
          .filter((v) => v.height)
          .map((v) => ({
            label: v.label || `${v.height}p`,
            height: v.height,
            ext: v.ext || "mp4",
            filesize: v.filesize || 0,
            approx: v.approx,
          }));
      }
      if (res?.ok === false) {
        const w = document.createElement("div");
        w.className = "md-floading";
        const error = String(res.error || "未知错误");
        w.textContent = /youtube.*sign in to confirm|sign in to confirm you're not a bot/i.test(error)
          ? "YouTube 要求验证。请先在浏览器登录，再点工具栏的 拾流下载器，开启“允许使用当前网站的登录信息”，然后重试。"
          : `无法读取画质：${error.slice(0, 120)}`;
        list.appendChild(w);
      } else {
        const recommended = formats.find(f=>!f.audio_only) || formats[0];
        if (recommended) {
          const info = document.createElement("div");
          info.className = "md-recommended";
          info.textContent = `${(res?.title || name).slice(0,90)} · ${recommended.label || (recommended.height ? `${recommended.height}p` : '视频')}`
            + (recommended.filesize ? ` · 约 ${humanSize(recommended.filesize)}` : ' · 大小下载时显示');
          list.appendChild(info);
        }
        list.appendChild(fmtRow(item, { best: true, label: recommended?.label,
          filesize: recommended?.filesize || 0, approx:recommended?.approx }, name));
        const details = document.createElement("details");
        details.innerHTML = `<summary>选择其他画质（${formats.length}）</summary>`;
        for (const f of formats) details.appendChild(fmtRow(item, f, name));
        if (formats.length) list.appendChild(details);
        if (isPage && formats.length <= 1 && /(^|\.)youtube\.com$|(^|\.)youtu\.be$/.test(location.hostname)) {
          const retry = document.createElement("button");
          retry.className = "md-retry";
          retry.textContent = "播放几秒后重新读取画质";
          retry.addEventListener("click", () => {
            retry.disabled = true;
            retry.textContent = "正在重新读取…";
            msg({ type: "probeMedia", url: item.url,
              opts: { media_type: "page", pageUrl: item.pageUrl || location.href } }).then((again) => {
              list.innerHTML = "";
              renderFormats(item, name, head, list, again);
            });
          });
          list.appendChild(retry);
        }
      }
      if (panel.classList.contains("md-open")) requestAnimationFrame(() => positionPanel());
    });
    return block;
  }

  function fmtRow(item, f, name) {
    const row = document.createElement("div");
    row.className = "md-fmt";
    const q = f.best ? "⭐ 推荐视频" : (f.label || (f.height ? `${f.height}p` : "格式"));
    const ext = f.best ? "" : (f.ext ? f.ext.toUpperCase() : "");
    const size = f.filesize ? (f.approx ? "~" : "") + humanSize(f.filesize) : "";
    let meta = [ext, size].filter(Boolean).join(" · ");
    if (f.needs_ffmpeg) meta += ` <span class="md-ff">⚙ 需要 ffmpeg 合并</span>`;
    else if (f.audio_only) meta += ` <span class="md-fa">🎵 仅音频</span>`;
    else if (f.best) meta = "自动选择可用的最佳画质";
    row.innerHTML = `
      <span class="md-fq">${escapeHtml(q)}</span>
      <span class="md-fmeta">${meta}</span>
      <button class="md-fdl" type="button" title="下载此画质">⬇</button>
    `;
    let sel = f.best ? { best: true } : {};   // mark ⭐ Best as an explicit choice
    if (!f.best) {
      if (f.audio_only) sel = { audio_only: true, format_id: f.format_id };
      else if (f.format_id) sel = { format_id: f.format_id };
      else if (f.height) sel = { height: f.height };
    }
    row.querySelector(".md-fdl").addEventListener(
      "click", (e) => doDownload(item, sel, e.currentTarget, name));
    return row;
  }

  async function doDownload(item, sel, btn, name) {
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = "…";
    const res = await downloadMedia(item, sel);
    btn.textContent = res && res.ok ? "✓" : "✗";
    const st = panel && panel.querySelector("#md-status");
    if (st) {
      if (res && res.ok) {
        st.textContent = `已加入队列：${res.filename || name}`;
        st.className = "md-status ok";
      } else {
        st.textContent = `失败：${(res && res.error) || "请检查桌面软件是否运行"}`;
        st.className = "md-status bad";
      }
    }
    setTimeout(() => {
      btn.textContent = old;
      btn.disabled = false;
    }, 1600);
  }

  function renderFormats(item, name, head, list, res) {
    const formats = (res && res.formats) || [];
    const recommended = formats.find((f) => !f.audio_only) || formats[0];
    if (res?.extractor) head.querySelector(".md-sub").textContent = `${res.extractor} · ${formats.length} 种画质`;
    if (recommended) {
      const info = document.createElement("div");
      info.className = "md-recommended";
      info.textContent = `${(res?.title || name).slice(0, 90)} · ${recommended.label || "视频"}`;
      list.appendChild(info);
    }
    list.appendChild(fmtRow(item, { best: true, label: recommended?.label }, name));
    for (const f of formats) list.appendChild(fmtRow(item, f, name));
  }

  function escapeHtml(s) {
    return (s || "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  // ── floating action button (draggable) ────────────────────────────────
  let fab = null;
  const FAB_POS_KEY = "md_fab_pos";
  let suppressNextClick = false;

  function buildFab(root) {
    fab = document.createElement("button");
    fab.id = "md-fab";
    fab.type = "button";
    fab.title = "点击打开下载面板，拖动可移动";
    fab.classList.add("offline");
    fab.innerHTML = `<span>⬇ MD</span><span class="md-fab-count" style="display:none"></span><span class="md-fab-progress"></span><span class="md-fab-tasks"></span>`;
    root.appendChild(fab);

    fab.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (suppressNextClick) {
        suppressNextClick = false;
        return; // this "click" was the end of a drag
      }
      togglePanel(fab);
    });

    makeDraggable(fab);
    restoreFabPos();
    // If the always-on floating button is off, start hidden — updateFab() will
    // reveal it whenever the page actually has something to download.
    if (cfg.showFloatingButton === false) fab.style.setProperty("display", "none", "important");
  }

  function clampFab(left, top) {
    const w = fab.offsetWidth || 52;
    const h = fab.offsetHeight || 52;
    return [
      Math.max(2, Math.min(window.innerWidth - w - 2, left)),
      Math.max(2, Math.min(window.innerHeight - h - 2, top)),
    ];
  }

  function setFabPos(left, top) {
    const [l, t] = clampFab(left, top);
    fab.style.setProperty("left", l + "px", "important");
    fab.style.setProperty("top", t + "px", "important");
    fab.style.setProperty("right", "auto", "important");
    fab.style.setProperty("bottom", "auto", "important");
  }

  function restoreFabPos() {
    try {
      B.storage.local.get(FAB_POS_KEY).then((r) => {
        const pos = r && r[FAB_POS_KEY];
        if (pos && typeof pos.left === "number") setFabPos(pos.left, pos.top);
      });
    } catch (_) {
      /* storage unavailable */
    }
  }

  // Pointer-event drag: setPointerCapture routes every pointermove/up to the
  // element itself — even when the pointer travels over a <video>, canvas or
  // cross-origin content that would otherwise swallow document mouse events.
  // `onStart(rect)` primes positioning; `onMove(dx,dy)` moves; `onEnd(moved)`
  // finalises. Returns nothing.
  function attachDrag(el, { onStart, onMove, onEnd }) {
    let dragging = false;
    let moved = false;
    let startX = 0;
    let startY = 0;
    let pid = null;

    el.addEventListener("pointerdown", (e) => {
      if (e.button != null && e.button !== 0) return; // left button / touch / pen only
      dragging = true;
      moved = false;
      startX = e.clientX;
      startY = e.clientY;
      pid = e.pointerId;
      try {
        el.setPointerCapture(pid);
      } catch (_) {
        /* capture unsupported → falls back to normal bubbling */
      }
      onStart(el.getBoundingClientRect());
      e.preventDefault();
      e.stopPropagation();
    });

    el.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) moved = true;
      onMove(dx, dy);
      e.preventDefault();
    });

    function finish(e) {
      if (!dragging) return;
      dragging = false;
      try {
        if (pid != null) el.releasePointerCapture(pid);
      } catch (_) {
        /* ignore */
      }
      pid = null;
      onEnd(moved);
    }
    el.addEventListener("pointerup", finish);
    el.addEventListener("pointercancel", finish);
  }

  function makeDraggable(el) {
    let origLeft = 0;
    let origTop = 0;
    attachDrag(el, {
      onStart: (rect) => {
        origLeft = rect.left;
        origTop = rect.top;
        setFabPos(rect.left, rect.top); // switch from right/bottom to left/top
      },
      onMove: (dx, dy) => setFabPos(origLeft + dx, origTop + dy),
      onEnd: (moved) => {
        if (!moved) return;
        suppressNextClick = true;
        const rect = el.getBoundingClientRect();
        try {
          B.storage.local.set({ [FAB_POS_KEY]: { left: rect.left, top: rect.top } });
        } catch (_) {
          /* ignore */
        }
        if (panel && panel.classList.contains("md-open")) positionPanel();
      },
    });
  }

  function positionPanel() {
    if (!panel) return;
    // The user dragged it somewhere — keep that spot, just clamp on-screen.
    if (panelPos) {
      setPanelPos(panelPos.left, panelPos.top);
      return;
    }
    if (!fab) return;
    const r = (panelAnchor?.isConnected ? panelAnchor : fab)?.getBoundingClientRect();
    if (!r) return;
    const pw = panel.offsetWidth || 340;
    const ph = panel.offsetHeight || 200;
    const gap = 8;
    const left = r.right + gap + pw <= window.innerWidth - gap
      ? r.right + gap : r.left - gap - pw >= gap
        ? r.left - gap - pw : r.right - pw;
    setPanelPos(left, Math.max(gap, Math.min(window.innerHeight - ph - gap, r.top)));
  }

  async function updateFab() {
    if (!fab) return;
    fab.classList.toggle("offline", !appOnline);
    const [res, status] = await Promise.all([getMedia(), msg({type: "downloadStatus"})]);
    const count = ((res && res.items) || []).length;
    const badge = fab.querySelector(".md-fab-count");
    const progress = fab.querySelector(".md-fab-progress");
    const tasks = fab.querySelector(".md-fab-tasks");
    const busy = ((status && status.jobs) || []).filter((job) =>
      ["Queued", "Connecting", "Downloading", "Processing"].includes(job.status));
    if (busy.length) {
      const total = busy.reduce((sum, job) => sum + (Number(job.total_size) || 0), 0);
      const done = busy.reduce((sum, job) => sum + (Number(job.downloaded) || 0), 0);
      const pct = total ? done / total * 100 : busy.reduce((sum, job) => sum + (Number(job.progress) || 0), 0) / busy.length;
      const shown = Math.max(0, Math.min(100, pct)).toFixed(0);
      badge.textContent = busy.length > 1 ? `${busy.length} · ${shown}%` : `${shown}%`;
      badge.style.display = "inline-block";
      progress.style.width = `${shown}%`;
      tasks.replaceChildren(...busy.slice(0, 6).map((job) => {
        const row = document.createElement("span");
        row.className = "md-task-row";
        const name = document.createElement("span");
        name.className = "md-task-name";
        name.textContent = job.filename || "下载任务";
        const percent = document.createElement("span");
        percent.className = "md-task-percent";
        percent.textContent = `${Math.max(0, Math.min(100, Number(job.progress) || 0)).toFixed(0)}%`;
        row.append(name, percent);
        return row;
      }));
      if (busy.length > 6) {
        const more = document.createElement("span");
        more.className = "md-task-more";
        more.textContent = `另有 ${busy.length - 6} 项`;
        tasks.appendChild(more);
      }
      fab.classList.add("md-downloading");
      fab.title = `后台下载中：${busy.length} 项 · ${shown}%`;
    } else {
      fab.classList.remove("md-downloading");
      progress.style.width = "0";
      tasks.replaceChildren();
      if (count > 0) {
        badge.textContent = count;
        badge.style.display = "inline-block";
      } else badge.style.display = "none";
    }
    // Always surface the button when there's something to grab — even if the
    // always-on floating button is turned off. This is the ONLY on-page control
    // when the media was sniffed from the network, or the player sits in an
    // iframe / uses a blob(MSE) src, so no per-<video> overlay could be placed.
    const show = cfg.showFloatingButton !== false || count > 0;
    if (show) fab.style.removeProperty("display");
    else fab.style.setProperty("display", "none", "important");
  }

  // ── per-<video> overlay button ────────────────────────────────────────
  function videoDirectItem(video) {
    // If the video element exposes a real http(s) src, offer it directly so
    // the button works even before/without network sniffing.
    let src = video.currentSrc || video.src || "";
    if (!src) {
      const source = video.querySelector("source[src]");
      if (source) src = source.src;
    }
    if (!src || !/^https?:\/\//i.test(src)) return null; // blob:/mediasource → rely on sniffing
    return {
      url: src,
      kind: /\.m3u8/i.test(src) ? "hls" : /\.mpd/i.test(src) ? "dash" : "file",
      mclass: "video",
      ext: (guessName(src).split(".").pop() || "").toLowerCase(),
      name: guessName(src),
      title: document.title || "",
      pageUrl: location.href,
      size: 0,
    };
  }

  function instagramItems(video, direct) {
    const article = video.closest('article') || video.closest('[role="dialog"]');
    const links = article ? [...article.querySelectorAll('a[href]')] : [];
    const permalink = links.map(a => a.href).find(h => /instagram\.com\/(?:reels?|p)\/[\w-]+/.test(h))
      || (/\/(?:reels?|p)\/[\w-]+/.test(location.pathname) ? location.href : '');
    const code = permalink.match(/\/(?:reels?|p)\/([\w-]+)/)?.[1];
    const caption = (article?.innerText || '').split('\n').map(x=>x.trim())
      .filter(x=>x && !/^(翻译|查看|关注|赞|评论|分享)/.test(x)).slice(0,6).join(' ').slice(0,85);
    if (!permalink) return []; // No reliable association: never guess from the tab's resource list.
    const source = direct?.url || permalink;
    let hash = 2166136261;
    for (const ch of new URL(source).pathname) hash = Math.imul(hash ^ ch.charCodeAt(0),16777619) >>> 0;
    const title = `${caption || 'Instagram视频'} [${code || hash.toString(16)}]`;
    return [{url:permalink,kind:'page',mclass:'video',size:0,ext:'mp4',title,name:title,pageUrl:permalink}];
  }

  function ensureOverlay(video) {
    if (videoOverlays.has(video)) return videoOverlays.get(video);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "md-video-overlay";
    btn.title = "下载当前视频；拖动可移动";
    btn.innerHTML = `⬇ 下载视频 <span class="md-badge" style="display:none"></span>`;
    btn._mdVideo = video;
    document.documentElement.appendChild(btn);
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (overlaySuppressClick) {
        overlaySuppressClick = false;
        return; // was the end of a drag
      }
      // Blob/MSE players expose no real URL, so seed the page item — the panel
      // then probes it and lists every quality (same as the toolbar popup).
      const direct = videoDirectItem(video);
      const fallback = direct || {
        url: location.href, kind: "page", mclass: "video",
        name: document.title || "当前页面的视频",
        title: document.title || "", pageUrl: location.href, size: 0,
        ambiguous: [...document.querySelectorAll("video")].filter((v) => {
          const r = v.getBoundingClientRect();
          return r.width >= MIN_VIDEO_W && r.height >= MIN_VIDEO_H && r.bottom > 0 && r.top < window.innerHeight;
        }).length > 1,
      };
      if (isInstagram) openPanel(instagramItems(video, direct), btn);
      else openPanel([fallback], btn);
    });
    makeVideoBtnDraggable(btn);
    videoOverlays.set(video, btn);
    return btn;
  }

  function overlayAnchor(rect) {
    // default top-right of the video, then the user's saved drag offset
    return {
      left: Math.max(6, Math.min(window.innerWidth - 130, rect.right - 128)) + videoBtnOffset.dx,
      top: Math.max(6, rect.top + 8) + videoBtnOffset.dy,
    };
  }

  function positionOverlay(video, btn) {
    if (overlayDragging) return; // don't fight the user's drag
    const rect = video.getBoundingClientRect();
    const visible =
      rect.width >= MIN_VIDEO_W &&
      rect.height >= MIN_VIDEO_H &&
      rect.bottom > 0 &&
      rect.right > 0 &&
      rect.top < window.innerHeight &&
      rect.left < window.innerWidth;
    if (!visible) {
      btn.classList.remove("md-show");
      return;
    }
    btn.classList.add("md-show");
    const a = overlayAnchor(rect);
    const w = btn.offsetWidth || 120;
    const h = btn.offsetHeight || 30;
    const left = Math.max(2, Math.min(window.innerWidth - w - 2, a.left));
    const top = Math.max(2, Math.min(window.innerHeight - h - 2, a.top));
    btn.style.setProperty("top", `${top}px`, "important");
    btn.style.setProperty("left", `${left}px`, "important");
  }

  function makeVideoBtnDraggable(el) {
    let origLeft = 0;
    let origTop = 0;
    attachDrag(el, {
      onStart: (rect) => {
        overlayDragging = true; // freeze positionOverlay() while we drag
        origLeft = rect.left;
        origTop = rect.top;
      },
      onMove: (dx, dy) => {
        const w = el.offsetWidth || 120;
        const h = el.offsetHeight || 30;
        const nl = Math.max(2, Math.min(window.innerWidth - w - 2, origLeft + dx));
        const nt = Math.max(2, Math.min(window.innerHeight - h - 2, origTop + dy));
        el.style.setProperty("left", nl + "px", "important");
        el.style.setProperty("top", nt + "px", "important");
      },
      onEnd: (moved) => {
        overlayDragging = false;
        if (!moved) return;
        overlaySuppressClick = true;
        // Save the offset relative to this video's default anchor so every
        // video button moves consistently and survives repositioning ticks.
        const video = el._mdVideo;
        if (!video) return;
        const vr = video.getBoundingClientRect();
        const baseLeft = Math.max(6, Math.min(window.innerWidth - 130, vr.right - 128));
        const baseTop = Math.max(6, vr.top + 8);
        const br = el.getBoundingClientRect();
        videoBtnOffset = { dx: Math.round(br.left - baseLeft), dy: Math.round(br.top - baseTop) };
        try {
          B.storage.local.set({ [VIDEO_BTN_OFFSET_KEY]: videoBtnOffset });
        } catch (_) {
          /* ignore */
        }
        repositionAll();
      },
    });
  }

  async function refreshOverlays() {
    if (pageUrl !== location.href) {
      pageUrl = location.href;
      lastReported = "";
      focusedItems = null;
      panelAnchor = null;
      closePanel();
    }
    if (!cfg.enabled || cfg.showVideoButton === false) {
      for (const [, btn] of videoOverlays) btn.remove();
      videoOverlays.clear();
      return 0;
    }
    const videos = Array.from(document.querySelectorAll("video"));
    if (videos.length) reportPageVideo();
    // Drop overlays for removed videos.
    for (const [vid, btn] of [...videoOverlays.entries()]) {
      if (!videos.includes(vid) || !document.contains(vid)) {
        btn.remove();
        videoOverlays.delete(vid);
      }
    }
    let count = 0;
    const res = await getMedia();
    const streamCount = ((res && res.items) || []).length;
    for (const video of videos) {
      const btn = ensureOverlay(video);
      positionOverlay(video, btn);
      const badge = btn.querySelector(".md-badge");
      if (streamCount > 0 && !isInstagram) {
        badge.textContent = streamCount;
        badge.style.display = "inline-block";
      } else {
        badge.style.display = "none";
      }
      count++;
    }
    return count;
  }

  function repositionAll() {
    for (const [video, btn] of videoOverlays.entries()) positionOverlay(video, btn);
  }

  // ── boot ──────────────────────────────────────────────────────────────
  async function build() {
    if (document.getElementById(ROOT_ID)) return;
    try {
      const c = await getConfig();
      if (c && typeof c === "object") cfg = { ...cfg, ...c };
    } catch (_) {
      /* use defaults */
    }
    if (cfg.enabled === false) return; // extension turned off in popup

    // Restore the saved on-video button drag offset.
    try {
      B.storage.local.get(VIDEO_BTN_OFFSET_KEY).then((r) => {
        const o = r && r[VIDEO_BTN_OFFSET_KEY];
        if (o && typeof o.dx === "number") videoBtnOffset = o;
      });
    } catch (_) {
      /* storage unavailable */
    }

    const cfgHost = document.createElement("div");
    cfgHost.id = ROOT_ID;
    document.documentElement.appendChild(cfgHost);

    buildPanel(cfgHost);
    buildFab(cfgHost); // always built; hidden until there's a download when the toggle is off
    updateFab();       // set its initial visibility right away

    document.addEventListener("click", (e) => {
      const root = document.getElementById(ROOT_ID);
      if (root && !root.contains(e.target)) closePanel();
    }, true);

    window.addEventListener("scroll", () => {
      repositionAll();
      if (panel && panel.classList.contains("md-open")) positionPanel();
    }, true);
    window.addEventListener("resize", () => {
      repositionAll();
      // Keep the panel inside the viewport when the window shrinks (auto-fit).
      if (panel && panel.classList.contains("md-open")) positionPanel();
    }, true);

    // Observe DOM for dynamically added <video> players.
    const mo = new MutationObserver(() => scheduleOverlayRefresh());
    mo.observe(document.documentElement, { childList: true, subtree: true });

    refreshStatus();
    refreshOverlays();
    setInterval(() => {
      resetForPageChange();
      refreshOverlays();
      updateFab();
    }, 1500);
  }

  let overlayTimer = null;
  function scheduleOverlayRefresh() {
    if (overlayTimer) return;
    overlayTimer = setTimeout(() => {
      overlayTimer = null;
      refreshOverlays();
    }, 400);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
})();
