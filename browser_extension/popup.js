const $ = (id) => document.getElementById(id);

// Firefox = promise-based `browser`; Chrome/Edge MV3 = promise-based `chrome`.
const B = (typeof browser !== "undefined" && browser) || chrome;

let activeTabId = null;
let activeTab = null;

const YTDLP_HOST_RE = /(^|\.)(youtube\.com|youtu\.be|vimeo\.com|dailymotion\.com|twitch\.tv|facebook\.com|instagram\.com|tiktok\.com|douyin\.com|xiaohongshu\.com|xinpianchang\.com|twitter\.com|x\.com|reddit\.com|bilibili\.com|soundcloud\.com)$/i;

function hostOf(url) {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

function send(payload) {
  return new Promise((resolve) => {
    try {
      const p = B.runtime.sendMessage(payload);
      if (p && typeof p.then === "function") {
        p.then((res) => resolve(res || { ok: false })).catch((error) => { console.error("[拾流链路] message_failed", String(error.message || error)); resolve({ ok: false }); });
      } else {
        resolve({ ok: false });
      }
    } catch (_) {
      resolve({ ok: false });
    }
  });
}

async function getActiveTab() {
  const [tab] = await B.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function load() {
  const cfg = await send({ type: "getConfig" });
  $("enabled").checked = !!cfg.enabled;
  $("captureDownloads").checked = !!cfg.captureDownloads;
  $("showVideoButton").checked = cfg.showVideoButton !== false;
  $("showFloatingButton").checked = cfg.showFloatingButton !== false;
  $("port").value = cfg.port || 7374;
  $("token").value = cfg.token || "";

  // Reflect the ACTUAL granted state of the optional `cookies` permission,
  // not a stored preference — the browser is the source of truth.
  try {
    $("sendCookies").checked = await B.permissions.contains({
      permissions: ["cookies"],
    });
  } catch (_) {
    $("sendCookies").checked = false;
  }

  const tab = await getActiveTab();
  activeTab = tab || null;
  activeTabId = tab ? tab.id : null;

  await recheck();
  await renderMedia();
}

async function recheck() {
  $("statusText").textContent = "正在连接软件…";
  $("dot").classList.remove("ok");
  const res = await send({ type: "ping" });
  if (res?.ok) {
    $("dot").classList.add("ok");
    $("statusText").textContent = "已连接桌面软件";
    $("statusDetail").textContent = `${res.name || "软件"} v${res.version || "?"} · 端口 ${res.port || ""}`;
    $("ffmpegWarn").style.display = res.ffmpeg === false ? "block" : "none";
  } else {
    $("dot").classList.remove("ok");
    $("statusText").textContent = "无法连接桌面软件";
    $("statusDetail").textContent = res?.error || "请先启动 拾流下载器";
  }
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

function mediaSub(item) {
  if (item.kind === "page") return "当前页面的视频 · 可选择画质";
  if (item.kind === "hls") return "HLS 视频流";
  if (item.kind === "dash") return "DASH 视频流";
  const size = item.size ? ` · ${humanSize(item.size)}` : "";
  return `${item.ext ? item.ext.toUpperCase() : "FILE"}${size}`;
}

function guessName(url) {
  try {
    const u = new URL(url);
    return decodeURIComponent(u.pathname.split("/").filter(Boolean).pop() || "download").split("?")[0];
  } catch {
    return "download";
  }
}

function esc(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

async function renderMedia() {
  const box = $("mediaList");
  const res = await send({ type: "getMedia", tabId: activeTabId });
  let items = (res && res.items) || [];

  // Fallback: on known video sites, always offer the page-download option even
  // if the content script hasn't reported yet (or is blocked on the page).
  const host = activeTab ? hostOf(activeTab.url) : "";
  if (/(^|\.)instagram\.com$/.test(host)) {
    if (!/\/(?:reels?|p)\/[\w-]+/.test(new URL(activeTab.url).pathname)) {
      box.textContent = '请点击具体视频旁的下载按钮，或打开该视频的独立帖子页面。';
      return;
    }
    items = items.filter(i=>i.kind==='page');
  }
  const hasPage = items.some((i) => i.kind === "page");
  if (!hasPage && host && YTDLP_HOST_RE.test(host) && /^https?:/i.test(activeTab.url || "")) {
    items = [
      {
        url: activeTab.url,
        kind: "page",
        mclass: "video",
        ext: "mp4",
        name: activeTab.title || "This page's video",
        title: activeTab.title || "",
        pageUrl: activeTab.url,
        size: 0,
      },
      ...items,
    ];
  }

  box.innerHTML = "";
  if (!items.length) {
    box.innerHTML =
      '<div class="empty">尚未识别到视频。请播放视频后重新打开扩展；也可右击视频链接下载。</div>';
    return;
  }
  const page = items.find((item) => item.kind === "page");
  if (page) {
    box.appendChild(renderItem(page));
    const others = items.filter((item) => item !== page);
    if (others.length) {
      const details = document.createElement("details");
      details.innerHTML = `<summary>其他检测到的资源（${others.length}）</summary>`;
      for (const item of others) details.appendChild(renderItem(item));
      box.appendChild(details);
    }
  } else {
    for (const item of items) box.appendChild(renderItem(item));
  }
}

function renderItem(item) {
  const block = document.createElement("div");
  block.className = "item-block";
  const kind = item.kind === "file" ? "file" : item.kind;
  const name = item.title || item.name || guessName(item.url);
  const isPage = item.kind === "page";
  const isStream = item.kind === "hls" || item.kind === "dash";
  const hasPicker = isPage || isStream;

  const head = document.createElement("div");
  head.className = "item";
  head.innerHTML = `
    <span class="kind ${kind}">${({page:'网页',file:'文件',hls:'视频流',dash:'视频流'})[item.kind] || '文件'}</span>
    <div class="meta">
      <div class="name" title="${esc(item.url)}">${esc(name)}</div>
      <div class="sub">${esc(mediaSub(item))}</div>
    </div>
    ${hasPicker ? "" : '<button class="get" type="button">下载</button>'}
  `;
  block.appendChild(head);

  if (!hasPicker) {
    head.querySelector(".get").addEventListener("click", (e) => doDownload(item, {}, e.currentTarget, name));
    return block;
  }

  // A real, scrollable list of every quality/format.
  const list = document.createElement("div");
  list.className = "formats";
  list.innerHTML = '<div class="floading">正在读取画质…</div>';
  block.appendChild(list);

  send({
    type: "probeMedia",
    tabId: activeTabId,
    url: item.url,
    opts: { media_type: isPage ? "page" : item.kind, pageUrl: item.pageUrl },
  }).then((res) => {
    list.innerHTML = "";
    if (res?.browser_diagnostic) {
      const diagnostic = document.createElement("div");
      diagnostic.className = "floading";
      diagnostic.textContent = res.browser_diagnostic;
      list.appendChild(diagnostic);
    }
    let formats = [];
    if (isPage) {
      formats = (res && res.formats) || [];
      if (res?.extractor) head.querySelector(".sub").textContent = `${res.extractor} · ${formats.length} 种画质`;
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
      w.className = "floading";
      const error = String(res.error || "未知错误");
      w.textContent = /youtube.*sign in to confirm|sign in to confirm you're not a bot/i.test(error)
        ? "YouTube 要求验证。请先在浏览器登录，并开启上方的“允许使用当前网站的登录信息”，然后重试。"
        : /the page needs to be reloaded/i.test(error)
          ? "YouTube 返回播放接口错误；请更新桌面软件和扩展后重试。"
        : `无法读取画质：${error.slice(0, 120)}`;
      list.appendChild(w);
    } else {
      const recommended = formats.find(f=>!f.audio_only) || formats[0];
      if (recommended) {
        const info = document.createElement('div');
        info.className = 'floading';
        info.textContent = `${(res?.title || name).slice(0,90)} · ${recommended.label || (recommended.height ? `${recommended.height}p` : '视频')}`
          + (recommended.filesize ? ` · 约 ${humanSize(recommended.filesize)}` : ' · 大小下载时显示');
        list.appendChild(info);
      }
      list.appendChild(fmtRow(item, { best:true,label:recommended?.label,
        filesize:recommended?.filesize || 0,approx:recommended?.approx }, name));
      const details = document.createElement("details");
      details.innerHTML = `<summary>选择其他画质（${formats.length}）</summary>`;
      for (const f of formats) details.appendChild(fmtRow(item, f, name));
      if (formats.length) list.appendChild(details);
    }
  });

  return block;
}

function fmtRow(item, f, name) {
  const row = document.createElement("div");
  row.className = "fmt";
  const q = f.best ? "⭐ 推荐视频" : (f.label || (f.height ? `${f.height}p` : "格式"));
  const ext = f.best ? "" : (f.ext ? f.ext.toUpperCase() : "");
  const size = f.filesize ? (f.approx ? "~" : "") + humanSize(f.filesize) : "";
  let meta = [ext, size].filter(Boolean).join(" · ");
  if (f.needs_ffmpeg) meta += ` <span class="ff">⚙ 需要 ffmpeg 合并</span>`;
  else if (f.audio_only) meta += ` <span class="fa">🎵 仅音频</span>`;
  else if (f.best) meta = "自动选择可用的最佳画质";
  row.innerHTML = `
    <span class="fq">${esc(q)}</span>
    <span class="fmeta">${meta}</span>
    <button class="fdl" type="button" title="下载此格式">⬇</button>
  `;
  let sel = f.best ? { best: true } : {};   // mark ⭐ Best as an explicit choice
  if (!f.best) {
    if (f.audio_only) sel = { audio_only: true, format_id: f.format_id };
    else if (f.format_id) sel = { format_id: f.format_id };
    else if (f.height) sel = { height: f.height };
  }
  row.querySelector(".fdl").addEventListener("click", (e) => doDownload(item, sel, e.currentTarget, name));
  return row;
}

async function doDownload(item, sel, btn, name) {
  console.info("[拾流链路] button_triggered", { kind: item.kind });
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "…";
  const res = await send({ type: "downloadMedia", item, sel, tabId: activeTabId });
  btn.textContent = res && res.ok ? "✓" : "✗";
  $("statusDetail").textContent =
    res && res.ok ? `已加入队列：${res.filename || name}` : `失败：${(res && res.error) || "请检查桌面软件是否运行"}`;
  setTimeout(() => {
    btn.textContent = old;
    btn.disabled = false;
  }, 1600);
}

$("save").addEventListener("click", async () => {
  const port = Math.max(1024, Math.min(65535, parseInt($("port").value, 10) || 7374));
  await B.storage.sync.set({
    enabled: $("enabled").checked,
    captureDownloads: $("captureDownloads").checked,
    showVideoButton: $("showVideoButton").checked,
    showFloatingButton: $("showFloatingButton").checked,
    port,
    token: $("token").value.trim(),
  });
  $("port").value = port;
  await recheck();
  $("statusDetail").textContent = "设置已保存";
});

// Explicit, in-gesture opt-in for reading cookies. Checking the box triggers
// the browser's own permission prompt; unchecking revokes it immediately.
$("sendCookies").addEventListener("change", async (e) => {
  const want = e.currentTarget.checked;
  try {
    if (want) {
      const granted = await B.permissions.request({ permissions: ["cookies"] });
      e.currentTarget.checked = granted;      // stay unchecked if the user declines
      $("statusDetail").textContent = granted
        ? "已允许使用当前网站的登录信息。"
        : "未授权登录信息，公开视频仍可尝试下载。";
    } else {
      await B.permissions.remove({ permissions: ["cookies"] });
      $("statusDetail").textContent = "已关闭登录信息访问。";
    }
  } catch (_) {
    e.currentTarget.checked = await B.permissions
      .contains({ permissions: ["cookies"] })
      .catch(() => false);
  }
  await renderMedia();
});

$("recheck").addEventListener("click", async () => {
  await recheck();
  await renderMedia();
});

function setupPinHint() {
  // Chrome hides newly-installed extensions in the puzzle (🧩) menu, so nudge
  // the user to pin us. Firefox already shows the toolbar icon — skip it there.
  const el = $("pinHint");
  if (!el || typeof browser !== "undefined") return;
  B.storage.local
    .get({ md_pin_hint_dismissed: false })
    .then((r) => { if (!r.md_pin_hint_dismissed) el.style.display = "block"; })
    .catch(() => {});
  const close = $("pinHintClose");
  if (close) {
    close.addEventListener("click", () => {
      el.style.display = "none";
      try { B.storage.local.set({ md_pin_hint_dismissed: true }); } catch (_) {}
    });
  }
}

$("extVersion").textContent = `v${B.runtime.getManifest().version}`;
load();
setupPinHint();
