/**
 * 拾流下载器 — browser service worker (Chrome / Edge / Brave, MV3)
 *
 * Behaviour:
 *   1. Sniff network traffic for video/audio (HLS .m3u8, DASH .mpd, progressive
 *      .mp4/.webm/.mp3 …) and remember what each tab is playing.
 *   2. Expose that list to the on-page button and the popup so the user can
 *      download the real stream — not just the page URL.
 *   3. Right-click menus + capture of normal browser downloads (unchanged).
 */

// Cross-browser alias: Firefox exposes promise-based `browser`; Chrome/Edge/
// Brave expose promise-based `chrome` (MV3). Event listeners work via `chrome`
// in both, so we only need this for the promise-returning calls below.
const B = (typeof browser !== "undefined" && browser) || chrome;

const DEFAULTS = {
  port: 7374,
  token: "",
  // OFF by default: taking over the browser's own downloads is a surprising,
  // heavy behaviour, so the user opts in explicitly from the popup.
  captureDownloads: false,
  minSizeBytes: 0, // 0 = capture all; set e.g. 102400 to skip tiny files
  enabled: true,
  showVideoButton: true,
  showFloatingButton: true,
};

// Prevent re-entrancy when we cancel browser downloads
const handledDownloadIds = new Set();

// Per-tab detected media:  tabId -> Map<dedupeKey, item>
const tabMedia = new Map();
const MAX_ITEMS_PER_TAB = 40;

// URL patterns we ask the browser to notify us about (keeps the listener cheap).
const MEDIA_FILTER = {
  urls: [
    "*://*/*.m3u8*",
    "*://*/*.m3u*",
    "*://*/*.mpd*",
    "*://*/*.mp4*",
    "*://*/*.ts*",
    "*://*/*.m4v*",
    "*://*/*.webm*",
    "*://*/*.mov*",
    "*://*/*.mkv*",
    "*://*/*.flv*",
    "*://*/*.mp3*",
    "*://*/*.m4a*",
    "*://*/*.aac*",
    "*://*/*.ogg*",
    "*://*/*.opus*",
    "*://*/*.flac*",
    "*://*/*.wav*",
  ],
};

const VIDEO_EXTS = ["mp4", "m4v", "webm", "mov", "mkv", "flv", "ts"];
const AUDIO_EXTS = ["mp3", "m4a", "aac", "ogg", "opus", "flac", "wav"];

// Junk we must never offer as a "video": thumbnails, avatars, UI sounds, ads,
// analytics. (This is what produced files like "no_input.mp3".)
const IGNORE_HOST_RE = /(^|\.)(ytimg\.com|ggpht\.com|googleusercontent\.com|gstatic\.com|doubleclick\.net|googlesyndication\.com|google-analytics\.com|scorecardresearch\.com|adservice\.google\.com)$/i;
const IGNORE_PATH_RE = /\/(s\/search|s\/player|generate_204|api\/stats|ptracking|pagead|log_event)/i;
const SMALL_FILE_BYTES = 50 * 1024; // progressive files below this are UI sounds/sprites

// Sites where the real media isn't a catchable file — always offer the whole
// page to yt-dlp instead. (Extra safety on top of "page has <video>".)
const YTDLP_HINT_HOST_RE = /(^|\.)(youtube\.com|youtu\.be|vimeo\.com|dailymotion\.com|twitch\.tv|facebook\.com|instagram\.com|tiktok\.com|douyin\.com|xiaohongshu\.com|xinpianchang\.com|twitter\.com|x\.com|reddit\.com|bilibili\.com|soundcloud\.com)$/i;

function isJunk(url, size, kind) {
  try {
    const u = new URL(url);
    if (IGNORE_HOST_RE.test(u.hostname)) return true;
    if (IGNORE_PATH_RE.test(u.pathname)) return true;
  } catch (_) {
    /* ignore */
  }
  if (kind === "file" && size && size < SMALL_FILE_BYTES) return true;
  return false;
}

chrome.runtime.onInstalled.addListener(async () => {
  const stored = await B.storage.sync.get(DEFAULTS);
  await B.storage.sync.set({ ...DEFAULTS, ...stored });
  rebuildMenus();
});

chrome.runtime.onStartup.addListener(rebuildMenus);

// ── media sniffing ────────────────────────────────────────────────────────

function extOf(url) {
  try {
    const u = new URL(url);
    const path = u.pathname.toLowerCase();
    const dot = path.lastIndexOf(".");
    return dot >= 0 ? path.slice(dot + 1) : "";
  } catch (_) {
    return "";
  }
}

function classify(url, contentType = "") {
  const ext = extOf(url);
  const low = url.toLowerCase();
  if (ext === "m3u8" || ext === "m3u" || low.includes(".m3u8")) {
    return { kind: "hls", mclass: "video" };
  }
  if (ext === "mpd" || low.includes(".mpd")) {
    return { kind: "dash", mclass: "video" };
  }
  if (VIDEO_EXTS.includes(ext)) return { kind: "file", mclass: "video" };
  if (AUDIO_EXTS.includes(ext)) return { kind: "file", mclass: "audio" };
  const mime = contentType.split(";", 1)[0].trim().toLowerCase();
  if (mime === "application/vnd.apple.mpegurl" || mime === "application/x-mpegurl")
    return { kind: "hls", mclass: "video", ext: "m3u8" };
  if (mime === "application/dash+xml")
    return { kind: "dash", mclass: "video", ext: "mpd" };
  if (mime === "video/mp4" || mime === "video/webm")
    return { kind: "file", mclass: "video", ext: mime.split("/")[1] };
  return null;
}

// Filename that looks like a streaming fragment rather than a whole file.
function looksLikeSegment(url) {
  try {
    const u = new URL(url);
    const name = (u.pathname.split("/").pop() || "").toLowerCase();
    if (/^\d+\.\w+$/.test(name)) return true; // 00012.mp4
    if (/(seg|segment|chunk|frag|fragment|init|media|part)[-_]?\d+/.test(name)) return true;
    if (/\.m4s$/.test(name) || /\.ts$/.test(name)) return true;
    return false;
  } catch (_) {
    return false;
  }
}

function guessTitleName(url, mclass) {
  try {
    const u = new URL(url);
    const last = decodeURIComponent(u.pathname.split("/").filter(Boolean).pop() || "");
    return last.split("?")[0] || (mclass === "audio" ? "audio" : "video");
  } catch (_) {
    return "video";
  }
}

function badgeColorFor(count) {
  return count > 0 ? "#2b579a" : "#00000000";
}

function updateBadge(tabId) {
  const media = tabMedia.get(tabId);
  const count = media ? (media.has("__page__") ? 1 : media.size) : 0;
  try {
    chrome.action.setBadgeBackgroundColor({ tabId, color: badgeColorFor(count) });
    chrome.action.setBadgeText({ tabId, text: count ? String(count) : "" });
  } catch (_) {
    /* tab may be gone */
  }
}

async function tabTitle(tabId) {
  try {
    const tab = await B.tabs.get(tabId);
    return { title: tab?.title || "", pageUrl: tab?.url || "" };
  } catch (_) {
    return { title: "", pageUrl: "" };
  }
}

async function addMedia(tabId, url, meta = {}) {
  try {
    const u = new URL(url);
    if (/(^|\.)(cdninstagram\.com|fbcdn\.net)$/.test(u.hostname)) {
      u.searchParams.delete('bytestart'); u.searchParams.delete('byteend');
      url = u.href; meta = {...meta, size:0};
    }
  } catch {}

  if (tabId < 0 || !/^https?:/i.test(url)) return;
  if (/^(text\/html|application\/json)/i.test(meta.contentType || "")) {
    const media = tabMedia.get(tabId);
    if (media?.delete(url.split("#")[0])) updateBadge(tabId);
    return;
  }
  const info = classify(url, meta.contentType || "");
  if (!info) return;
  const ext = info.ext || extOf(url);

  let media = tabMedia.get(tabId);
  if (!media) {
    media = new Map();
    tabMedia.set(tabId, media);
  }

  // Reject (and remove) junk like thumbnails / UI sounds / tiny sprites.
  if (isJunk(url, meta.size, info.kind)) {
    const jk = url.split("#")[0];
    if (media.has(jk)) {
      media.delete(jk);
      updateBadge(tabId);
    }
    return;
  }

  const hasStream = [...media.values()].some((m) => m.kind === "hls" || m.kind === "dash");
  const segDir = url.split("#")[0].split("?")[0].replace(/\/[^/]*$/, "/");

  if (info.kind === "hls" || info.kind === "dash") {
    // A manifest arrived — drop any progressive "files" already collected;
    // on a streaming page those were almost certainly fragments.
    for (const [k, v] of [...media.entries()]) {
      if (v.kind === "file") media.delete(k);
    }
  } else if (ext === "ts") {
    // Raw MPEG-TS (.ts). When the tab already has a manifest, these are its
    // fragments and the manifest is the better download — so skip them.
    // Otherwise offer the stream ONCE: collapse every .ts that shares a folder
    // into a single entry, so a site that exposes only .ts is downloadable
    // without flooding the list with every segment.
    if (hasStream) return;
    for (const v of media.values()) {
      if (v.ext === "ts" && v._segDir === segDir) return;
    }
  } else {
    // Progressive file. Skip obvious fragments, and skip entirely if this tab
    // is already streaming (HLS/DASH) — those .mp4/.webm hits are fragments.
    if (hasStream || looksLikeSegment(url)) return;
  }

  const key = url.split("#")[0];
  if (media.has(key)) {
    if (meta.size) media.get(key).size = meta.size;
    return;
  }
  if (media.size >= MAX_ITEMS_PER_TAB) return;

  const tab = await tabTitle(tabId);
  const title = media.get("__page__")?.title || tab.title;
  const pageUrl = tab.pageUrl;
  media.set(key, {
    url,
    kind: info.kind,
    mclass: info.mclass,
    ext,
    size: meta.size || 0,
    contentType: meta.contentType || "",
    name: ext === "ts" ? (title || "Video stream (.ts)") : guessTitleName(url, info.mclass),
    title: title || "",
    pageUrl: pageUrl || meta.pageUrl || "",
    _segDir: ext === "ts" ? segDir : undefined,
    ts: Date.now(),
  });
  updateBadge(tabId);
}

chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    if (details.tabId >= 0) addMedia(details.tabId, details.url);
  },
  MEDIA_FILTER
);

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (details.tabId < 0 || details.statusCode < 200 || details.statusCode >= 300) return;
    let size = 0;
    let ctype = "";
    for (const h of details.responseHeaders || []) {
      const n = h.name.toLowerCase();
      if (n === "content-length") size = parseInt(h.value, 10) || 0;
      else if (n === "content-type") ctype = (h.value || "").toLowerCase();
    }
    // Players often use extensionless URLs; the response MIME is the only clue.
    if (!classify(details.url, ctype)) return;
    addMedia(details.tabId, details.url, { size, contentType: ctype });
  },
  { urls: ["<all_urls>"] },
  ["responseHeaders"]
);

// Reset a tab's media when it navigates to a new page.
chrome.webNavigation.onCommitted.addListener((details) => {
  if (details.frameId === 0) {
    tabMedia.delete(details.tabId);
    updateBadge(details.tabId);
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  tabMedia.delete(tabId);
});

// A "page" item = "download whatever video this page is showing" via yt-dlp.
// Reported by the content script when it sees a <video>, or inferred from the
// hostname. This is the ONLY path that works on such sites.
function setPageItem(tabId, pageUrl, title) {
  if (tabId < 0 || !/^https?:/i.test(pageUrl || "")) return;
  let media = tabMedia.get(tabId);
  if (!media) {
    media = new Map();
    tabMedia.set(tabId, media);
  }
  media.set("__page__", {
    url: pageUrl,
    kind: "page",
    mclass: "video",
    ext: "mp4",
    size: 0,
    name: title || "当前页面的视频",
    title: title || "",
    pageUrl,
    isPage: true,
    ts: Date.now(),
  });
  for (const item of media.values()) {
    if (item.kind !== "page" && item.mclass === "video") item.title = title || item.title;
  }
  updateBadge(tabId);
}

function mediaList(tabId) {
  const media = tabMedia.get(tabId);
  if (!media) return [];
  const page = media.get("__page__");
  // Page video first, then streams, then largest files.
  return [...media.values()].filter((m) => (!page || m.mclass !== "audio")).sort((a, b) => {
    const rank = (m) => (m.kind === "page" ? 0 : m.kind === "hls" || m.kind === "dash" ? 1 : 2);
    if (rank(a) !== rank(b)) return rank(a) - rank(b);
    return (b.size || 0) - (a.size || 0);
  });
}

// ── context menus ─────────────────────────────────────────────────────────

function rebuildMenus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "md-link",
      title: "使用 拾流下载器 下载",
      contexts: ["link"],
    });
    chrome.contextMenus.create({
      id: "md-page",
      title: "下载当前页面视频",
      contexts: ["page"],
    });
    chrome.contextMenus.create({
      id: "md-media",
      title: "使用 拾流下载器 下载视频或媒体",
      contexts: ["image", "video", "audio"],
    });
    chrome.contextMenus.create({
      id: "md-selection",
      title: "下载选中的链接",
      contexts: ["selection"],
    });
  });
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  let url = "";
  if (info.menuItemId === "md-link") url = info.linkUrl || "";
  else if (info.menuItemId === "md-page") url = info.pageUrl || tab?.url || "";
  else if (info.menuItemId === "md-media") url = info.srcUrl || "";
  else if (info.menuItemId === "md-selection") {
    const t = (info.selectionText || "").trim();
    if (/^https?:\/\//i.test(t)) url = t;
  }
  if (!url || !/^https?:\/\//i.test(url)) {
    notify("拾流下载器", "未找到有效的网页链接。");
    return;
  }
  const referrer = info.pageUrl || tab?.url || "";
  const info2 = classify(url);
  await sendToApp(url, {
    referrer,
    pageUrl: referrer,
    filename: guessName(url),
    media_type: info2 ? info2.kind : "http",
    title: tab?.title || "",
  });
});

// ── capture browser downloads (unchanged behaviour) ────────────────────────

// A brand-new download begins in this state; anything else is history.
// startTime within this window of "now" is what tells a fresh download apart
// from a replayed one.
const FRESH_DOWNLOAD_MS = 15000;

chrome.downloads.onCreated.addListener(async (item) => {
  const cfg = await getConfig();
  if (!cfg.enabled || !cfg.captureDownloads) return;
  if (!item || !item.id) return;
  if (handledDownloadIds.has(item.id)) return;

  // MV3 service workers are ephemeral. Every time this one wakes — including
  // when the desktop app connects — Chrome RE-FIRES onCreated for the downloads
  // already in its list, i.e. the recent history, not just new downloads. And
  // handledDownloadIds lives in memory, so it's empty after each wake and can't
  // dedupe them. Left unguarded, that replays the whole history at the app: with
  // the app running it re-queues every past download; with it off, each hand-off
  // fails and the offline toast storms. So: only a download that is actually
  // starting right now. A completed/interrupted item, or one whose startTime is
  // more than a few seconds old, is history — ignore it.
  if (item.state && item.state !== "in_progress") return;
  const startedAt = item.startTime ? Date.parse(item.startTime) : NaN;
  if (!Number.isNaN(startedAt) && Date.now() - startedAt > FRESH_DOWNLOAD_MS) return;

  const url = item.finalUrl || item.url || "";
  if (!url || !/^https?:\/\//i.test(url)) return;
  if (url.startsWith("blob:") || url.startsWith("data:") || url.startsWith("chrome")) return;
  if (item.filename && item.filename.includes("MagicDownloader") && item.state === "complete") return;

  handledDownloadIds.add(item.id);
  try {
    // Check the app is actually reachable BEFORE touching the browser's
    // download. The old order cancelled + erased first, then tried to hand off:
    // if the app was off, the file was destroyed and gone (and every capture
    // fired an offline toast). Now, when the app isn't running we leave the
    // browser to download normally and just say so, once.
    const health = await pingApp();
    if (!health.ok) {
      handledDownloadIds.delete(item.id);
      notifyOffline();
      return;
    }

    let finalUrl = url;
    if (!item.finalUrl) {
      await sleep(150);
      try {
        const [fresh] = await B.downloads.search({ id: item.id });
        if (fresh?.finalUrl) finalUrl = fresh.finalUrl;
        if (fresh?.fileSize > 0 && cfg.minSizeBytes > 0 && fresh.fileSize < cfg.minSizeBytes) {
          handledDownloadIds.delete(item.id);
          return;
        }
      } catch (_) {
        /* ignore */
      }
    }

    await B.downloads.cancel(item.id).catch(() => {});
    await B.downloads.erase({ id: item.id }).catch(() => {});

    const filename = basename(item.filename) || guessName(finalUrl);
    const referrer = item.referrer || "";
    const info = classify(finalUrl);
    await sendToApp(finalUrl, {
      referrer,
      filename,
      media_type: info ? info.kind : "http",
    });
  } catch (err) {
    console.error("拾流下载器 capture failed", err);
    handledDownloadIds.delete(item.id);
  }

  if (handledDownloadIds.size > 200) handledDownloadIds.clear();
});

// ── message bus ─────────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type === "downloadMedia" || msg?.type === "add") {
    console.info("[拾流链路] extension_received", { type: msg.type, tabId: sender?.tab?.id ?? msg.tabId });
  }
  if (msg?.type === "ping") {
    pingApp().then(sendResponse);
    return true;
  }
  if (msg?.type === "add" && msg.url) {
    sendToApp(msg.url, msg.opts || {}).then(sendResponse);
    return true;
  }
  if (msg?.type === "getConfig") {
    getConfig().then(sendResponse);
    return true;
  }
  if (msg?.type === "getMedia") {
    const tabId = msg.tabId != null ? msg.tabId : sender?.tab?.id;
    sendResponse({ ok: true, items: tabId != null ? mediaList(tabId) : [] });
    return false;
  }
  if (msg?.type === "downloadStatus") {
    downloadStatus().then(sendResponse);
    return true;
  }
  if (msg?.type === "reportPageVideo") {
    const tabId = sender?.tab?.id;
    if (tabId != null) setPageItem(tabId, msg.pageUrl, msg.title);
    sendResponse({ ok: true });
    return false;
  }
  if (msg?.type === "downloadMedia" && msg.item) {
    const tabId = msg.tabId != null ? msg.tabId : sender?.tab?.id;
    downloadMedia(msg.item, msg.sel || {}, tabId).then(sendResponse, (error) => {
      console.error("[拾流链路] extension_failed", String(error.message || error));
      sendResponse({ ok: false, error: String(error.message || error) });
    });
    return true;
  }
  if (msg?.type === "probeMedia" && msg.url) {
    const tabId = sender?.tab?.id ?? msg.tabId;
    probeMedia(msg.url, msg.opts || {}, tabId).then(sendResponse);
    return true;
  }
  if (msg?.type === "probeBrowserYouTube" && msg.url) {
    const tabId = sender?.tab?.id ?? msg.tabId;
    browserYouTube(msg.url, tabId).then(captured => sendResponse({ok:true,
      formats:captured.formats.map(({url,audio_url,...f}) => f),
      title:captured.title, extractor:"浏览器"}));
    return true;
  }
  if (msg?.type === "previewDestination" && msg.item?.url) {
    previewDestination(msg.item).then(sendResponse);
    return true;
  }
  return false;
});

async function downloadMedia(item, sel, tabId) {
  if (item.kind === "page" && /(^|\.)(missav\.ai|xinpianchang\.com|douyin\.com)$/.test(new URL(item.url).hostname)
      && (sel.best || sel.format_id === "browser:detected" || sel.height)) {
    const media = await detectedPageMedia(tabId, item.url);
    if (!media) return {ok:false,error:"未找到当前页面的完整视频资源，请播放后重试"};
    return sendToApp(media.url, {filename:filenameForMedia(item,{}),title:item.title || "",
      pageUrl:item.url,referrer:item.url,media_type:media.kind === "file" ? "http" : media.kind,
      height:sel.height});
  }
  if (item.kind === "page" && /(^|\.)91porn\.com$/.test(new URL(item.url).hostname)
      && (sel.best || sel.format_id === "browser:direct")) {
    const media = await browserPageMedia(item.url, tabId);
    if (!media) return {ok:false,error:"播放器尚未提供视频地址，请播放几秒后重试"};
    return sendToApp(media.url, {filename:filenameForMedia(item,{}),title:item.title || "",
      pageUrl:item.url,referrer:item.url,media_type:media.kind});
  }
  if (item.kind === "page" && youtubeVideoId(item.url)
      && (sel.best || String(sel.format_id || "").startsWith("browser:"))) {
    const captured = await browserYouTube(item.url, tabId);
    // yt-dlp can often expose HLS/DASH HD tracks even when the browser's
    // player response contains only the old 360p combined MP4.
    if (sel.best) {
      const page = await probeYouTubeApp(item.url, {media_type:"page",pageUrl:item.url});
      if (page?.ok && (page.formats || []).some(f => Number(f.height) > Number(captured.formats[0]?.height || 0))) {
        return sendToApp(item.url, {
          filename:filenameForMedia(item,sel),title:item.title || "",pageUrl:item.url,
          referrer:item.url,media_type:"page",best:true,
        });
      }
    }
    const chosen = sel.best ? captured.formats[0]
      : captured.formats.find((f) => f.format_id === sel.format_id);
    if (chosen) {
      if (chosen.audio_url) return sendToApp(item.url, {
        filename:`${sanitize(captured.title || item.title || 'video').slice(0,120)}.mp4`,
        title:captured.title || item.title, pageUrl:item.url, referrer:item.url,
        media_type:'page', cookie:'', browser_player_url:captured.player_url || '',
        browser_pair:[chosen.url,chosen.audio_url],height:chosen.height,
      });
      return sendToApp(chosen.url, {
        filename: `${sanitize(captured.title || item.title || "video").slice(0, 120)}.${chosen.ext}`,
        title: captured.title || item.title, pageUrl: item.url, referrer: item.url,
        media_type: "http", cookie: "", browser_player_url: captured.player_url || "",
      });
    }
    if (!sel.best) return { ok: false, error: "此画质的浏览器地址已失效，请重新打开下载面板。" };
  }
  const opts = {
    filename: filenameForMedia(item, sel),
    referrer: item.pageUrl || "",
    pageUrl: item.pageUrl || "",
    title: item.title || "",
    media_type: item.kind === "file" ? "http" : item.kind, // page | hls | dash | http
  };
  if (sel) {
    if (sel.format_id) opts.format_id = sel.format_id;
    if (sel.height) opts.height = sel.height;
    if (sel.audio_only) opts.audio_only = true;
    if (sel.best) opts.best = true;   // explicit ⭐ Best — app won't re-ask quality
  }
  return sendToApp(item.url, opts);
}

function filenameForMedia(item, sel) {
  const base = sanitize(item.title || item.name || "video")
    .replace(/\.(mp4|m4v|webm|mkv|mov|avi|flv|ts|m3u8|mpd)$/i, "").slice(0, 120);
  if (sel && sel.audio_only) return `${base}.m4a`;
  if (item.kind === "hls" || item.kind === "dash" || item.kind === "page") {
    return `${base}.mp4`;
  }
  return `${base}.${item.ext || "bin"}`;
}

function youtubeVideoId(url) {
  try {
    const u = new URL(url);
    if (!["www.youtube.com", "youtube.com", "m.youtube.com"].includes(u.hostname)) return null;
    const id = u.pathname === "/watch" ? u.searchParams.get("v") : u.pathname.match(/^\/shorts\/([^/]+)$/)?.[1];
    return /^[\w-]{11}$/.test(id || "") ? id : null;
  } catch { return null; }
}

// Executed only on an explicit probe/download in the selected YouTube tab.
// Returns playback metadata; never reads document.cookie or browser storage.
function readYouTubePlayer(expectedId) {
  const player = document.querySelector("#movie_player");
  let live;
  try { live = player?.getPlayerResponse?.(); } catch {}
  const sources = [live, document.querySelector("ytd-watch-flexy")?.playerData, window.ytInitialPlayerResponse];
  const responses = sources.filter((r) => r?.videoDetails?.videoId === expectedId);
  const formats = [];
  const adaptive = [];
  for (const r of responses) {
    for (const f of (r.streamingData?.adaptiveFormats || []).slice(0, 100)) {
      if (typeof f.url === 'string') adaptive.push({url:f.url,itag:f.itag,height:f.height,
        mimeType:f.mimeType,size:f.contentLength,bitrate:f.bitrate});
    }
    for (const f of (r.streamingData?.formats || []).slice(0, 40)) {
      // Only combined video+audio files belong on the plain HTTP path.
      if (typeof f.url !== "string" || !f.height || !(f.audioQuality || f.audioChannels)) continue;
      formats.push({ url: f.url, itag: f.itag, height: f.height, mimeType: f.mimeType,
        size: f.contentLength });
    }
  }
  return {
    player_url: [...document.querySelectorAll('script[src]')].map((s) => s.src)
      .find((s) => /^https:\/\/www\.youtube\.com\/s\/player\/[^?#]+\/base\.js(?:[?#]|$)/.test(s)) || "",
    videoId: expectedId, title: String(responses[0]?.videoDetails?.title || "").slice(0, 200), formats, adaptive,
    responseCount: responses.length,
    adaptiveCount: Math.max(0, ...responses.map((r) => (r.streamingData?.adaptiveFormats || []).length)),
    sabr: responses.some((r) => !!r.streamingData?.serverAbrStreamingUrl),
  };
}

function normalizeYouTubeCapture(data, expectedId) {
  const formats = [];
  if (data?.videoId !== expectedId) return { formats, diagnostic: "浏览器视频已切换，请重新打开面板。" };
  for (const f of (Array.isArray(data.formats) ? data.formats : []).slice(0, 80)) {
    try {
      const u = new URL(f.url);
      if (u.protocol !== "https:" || !u.hostname.endsWith(".googlevideo.com") || u.username || u.password) continue;
      if (u.searchParams.has("range") || u.searchParams.has("sq")) continue;
      const ext = /^video\/(mp4|webm)(?:;|$)/i.exec(f.mimeType || "")?.[1]?.toLowerCase();
      const height = Number(f.height), itag = Number(f.itag);
      if (!ext || !Number.isInteger(height) || height < 1 || height > 8640 || !Number.isInteger(itag)) continue;
      if (formats.some((p) => p.format_id === `browser:${itag}`)) continue;
      formats.push({ format_id: `browser:${itag}`, url: u.href, height, ext,
        filesize: Math.max(0, Number(f.size) || 0), label: `${height}p · 浏览器音画直链` });
    } catch {}
  }
  const adaptive = (Array.isArray(data.adaptive) ? data.adaptive : []).filter(f => {
    try { const u = new URL(f.url); return u.protocol === 'https:' && u.hostname.endsWith('.googlevideo.com')
      && !u.username && !u.password && !u.searchParams.has('range') && !u.searchParams.has('sq'); } catch { return false; }
  });
  const audio = adaptive.filter(f=>/^audio\/mp4[;]?/.test(f.mimeType || '')).sort((a,b)=>(b.bitrate||0)-(a.bitrate||0))[0];
  if (audio) for (const v of adaptive) {
    if (!/^video\/mp4[;]?/.test(v.mimeType || '') || !Number.isInteger(v.height) || v.height < 1) continue;
    const id = `browser:${v.itag}+${audio.itag}`;
    if (formats.some(f=>f.format_id===id)) continue;
    formats.push({format_id:id,url:v.url,audio_url:audio.url,height:v.height,ext:'mp4',needs_ffmpeg:true,
      filesize:(Number(v.size)||0)+(Number(audio.size)||0), label:`${v.height}p · 音画自动合并`});
  }
  formats.sort((a, b) => b.height - a.height);
  const diagnostic = formats.length ? `浏览器已识别 ${formats.length} 个视频画质（分离音轨将自动合并）。`
    : data.sabr ? "浏览器脚本已运行：当前视频使用 SABR 播放，本版尚未接入该协议；正在尝试网页解析。"
    : data.adaptiveCount ? "浏览器脚本已运行：未取得可配对的完整 MP4 音视频地址；正在尝试网页解析。"
    : data.responseCount ? "浏览器脚本已运行，但播放器数据没有可用视频地址；正在尝试网页解析。"
    : "浏览器脚本已运行，尚未取得当前视频的播放器数据；正在尝试网页解析。";
  return { formats, title: String(data.title || "").slice(0, 200), diagnostic,
    player_url: typeof data.player_url === "string" ? data.player_url.slice(0, 1000) : "" };
}

async function browserYouTube(url, tabId) {
  const empty = (diagnostic) => ({ formats: [], diagnostic });
  const expectedId = youtubeVideoId(url);
  if (!expectedId || !Number.isInteger(tabId)) return empty("未定位到 YouTube 视频标签页。");
  try {
    const tab = await B.tabs.get(tabId);
    if (youtubeVideoId(tab.url) !== expectedId) return empty("浏览器视频已切换，请重新打开面板。");
    const result = await B.scripting.executeScript({ target: { tabId, frameIds: [0] },
      world: "MAIN", func: readYouTubePlayer, args: [expectedId] });
    return normalizeYouTubeCapture(result?.[0]?.result, expectedId);
  } catch {
    return empty("未能运行浏览器读取脚本，请检查 拾流下载器扩展的网站访问权限及是否已重新加载。");
  }
}

async function probeMedia(url, opts, tabId) {
  if (!youtubeVideoId(url)) {
    let result = /(^|\.)douyin\.com$/.test(new URL(url).hostname) ? null : await probeApp(url, opts);
    if (/(^|\.)(missav\.ai|xinpianchang\.com|douyin\.com)$/.test(new URL(url).hostname)) {
      const media = await detectedPageMedia(tabId,url);
      if (media) {
        const direct = media.kind === 'hls' || media.kind === 'dash'
          ? await probeApp(media.url,{media_type:media.kind,pageUrl:url}) : null;
        const variants = (direct?.ok ? direct.variants || [] : []).filter(v=>v.height);
        const candidate = {format_id:"browser:detected",label:variants.length
          ? `播放器视频 · ${Math.max(...variants.map(v=>v.height))}p`
          : media.kind === "hls" ? "播放器 HLS 视频" : "播放器 MP4 视频",
          ext:media.kind === "hls" ? "mp4" : media.ext || "mp4",
          filesize:media.size || Math.max(0,...variants.map(v=>Number(v.filesize)||0)),
          approx:!media.size};
        return {ok:true,extractor:"浏览器识别",title:media.title || "",
          formats:[candidate,...variants.map(v=>({label:v.label || `${v.height}p`,height:v.height,
            ext:v.ext || 'mp4',filesize:v.filesize || 0,approx:v.approx})),
            ...(result?.ok ? result.formats || [] : [])]};
      }
    }
    result = result || await probeApp(url, opts);
    if (result?.ok || !/91porn\.com$/.test(new URL(url).hostname)) return result;
    const captured = await browserPageMedia(url, tabId);
    if (!captured) return result;
    return {ok:true,extractor:"浏览器播放器",formats:[{format_id:"browser:direct",label:"播放器视频",ext:captured.ext}]};
  }
  const [captured, result] = await Promise.all([
    browserYouTube(url, tabId), probeYouTubeApp(url, opts),
  ]);
  if (result?.ok && result.formats?.length) return {...result,
    formats:[...result.formats,...captured.formats.map(({url,audio_url,...f})=>f)],
    browser_diagnostic:captured.diagnostic};
  if (captured.formats.length) return { ok: true, formats: captured.formats.map(({ url, audio_url, ...f }) => f),
    extractor: "浏览器", browser_diagnostic: result?.error
      ? `${captured.diagnostic} 更多画质未能读取：${result.error}。可在扩展弹窗开启“允许使用当前网站的登录信息”后重试。`
      : captured.diagnostic };
  return { ...result, browser_diagnostic: captured.diagnostic };
}

function douyinVideoId(url) {
  const u = new URL(url);
  return /(^|\.)douyin\.com$/.test(u.hostname)
    ? u.pathname.match(/\/video\/(\d+)/)?.[1] || u.searchParams.get('modal_id') || '' : '';
}

async function detectedPageMedia(tabId, pageUrl) {
  if (/(^|\.)douyin\.com$/.test(new URL(pageUrl).hostname)) {
    if (!Number.isInteger(tabId)) return null;
    try {
      const tab = await B.tabs.get(tabId);
      if (new URL(tab.url).origin !== new URL(pageUrl).origin ||
          douyinVideoId(tab.url) !== douyinVideoId(pageUrl)) return null;
      const playing = await browserPageMedia(pageUrl, tabId);
      if (playing) return playing;
    } catch { return null; }
  }
  return recommendedDetected(tabId, pageUrl);
}

function recommendedDetected(tabId,pageUrl) {
  const videoId = douyinVideoId(pageUrl);
  const candidates = mediaList(tabId).filter(m => m.kind !== 'page' && m.mclass === 'video'
    && m.pageUrl && new URL(m.pageUrl).origin === new URL(pageUrl).origin
    && (!videoId || douyinVideoId(m.pageUrl) === videoId));
  // ponytail: blob players fall back to largest same-video page capture; use
  // player metadata if preloaded clips cannot be distinguished on that page.
  return candidates.find(m => m.kind === 'hls') || candidates.find(m => m.kind === 'dash') ||
    candidates.find(m => m.kind === 'file' && (videoId || m.size >= 1024*1024 || candidates.length === 1)) || null;
}

// Read the current player's chosen source only after an explicit probe/click.
// The generic network list may contain ads and unrelated page media.
function readPagePlayer() {
  const videos = [...document.querySelectorAll('video')];
  const visible = videos.filter(v => v.getBoundingClientRect().width > 150 &&
    v.getBoundingClientRect().height > 90);
  const playing = visible.find(v => !v.paused && v.currentTime > 0);
  const video = playing || visible[0];
  let url = video?.currentSrc || video?.src ||
    video?.querySelector('source[src]')?.src || '';
  if (!/^https?:\/\//i.test(url)) {
    try { url = window.jwplayer?.()?.getPlaylistItem?.()?.file || ''; } catch {}
  }
  return /^https?:\/\//i.test(url) ? url : '';
}

async function browserPageMedia(pageUrl, tabId) {
  if (!Number.isInteger(tabId)) return null;
  try {
    const tab = await B.tabs.get(tabId);
    if (new URL(tab.url).origin !== new URL(pageUrl).origin ||
        new URL(tab.url).searchParams.get('viewkey') !== new URL(pageUrl).searchParams.get('viewkey')) return null;
    const found = await B.scripting.executeScript({target:{tabId,allFrames:true},
      world:'MAIN',func:readPagePlayer});
    for (const entry of found || []) {
      const url = entry.result;
      if (!url) continue;
      const media = classify(url);
      if (media?.mclass === 'video') return {url,kind:media.kind,ext:media.ext || extOf(url) || 'mp4'};
      // A signed player URL can omit the file extension; the player itself
      // establishes that it is a video source.
      if (/^https?:\/\//i.test(url)) return {url,kind:'http',ext:'mp4'};
    }
  } catch {}
  return null;
}

function sanitize(s) {
  return (s || "").replace(/[<>:"/\\|?*\n\r\t]+/g, "_").trim() || "video";
}

// ── app API ─────────────────────────────────────────────────────────────────

async function getConfig() {
  return { ...DEFAULTS, ...(await B.storage.sync.get(DEFAULTS)) };
}

async function apiBase() {
  const cfg = await getConfig();
  return `http://127.0.0.1:${cfg.port}`;
}

async function downloadStatus() {
  try {
    const res = await fetch(`${await apiBase()}/api/status`, {headers:await authHeaders()});
    const data = await res.json();
    return {ok:res.ok,jobs:data.jobs || []};
  } catch { return {ok:false,jobs:[]}; }
}

async function authHeaders() {
  const cfg = await getConfig();
  const h = { "Content-Type": "application/json" };
  if (cfg.token) {
    h["X-Magic-Token"] = cfg.token;
    h["Authorization"] = `Bearer ${cfg.token}`;
  }
  return h;
}

async function pingApp() {
  try {
    const base = await apiBase();
    const res = await fetch(`${base}/api/ping`, { method: "GET" });
    if (!res.ok) return { ok: false, error: `HTTP ${res.status}` };
    const data = await res.json();
    return { ok: true, ...data };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

async function probeApp(url, opts = {}) {
  try {
    const base = await apiBase();
    const cookie = opts.cookie ?? (await collectCookies(url));
    const res = await fetch(`${base}/api/probe`, {
      method: "POST",
      headers: await authHeaders(),
      body: JSON.stringify({
        url,
        media_type: opts.media_type || "",
        referrer: opts.referrer || opts.pageUrl || "",
        page_url: opts.pageUrl || "",
        user_agent: navigator.userAgent || "",
        cookie,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) return { ok: false, error: readableError(data.error || `HTTP ${res.status}`) };
    return { ok: true, ...data };
  } catch (e) {
    return { ok: false, error: readableError(String(e.message || e)) };
  }
}

const youtubeProbeCache = new Map();
function probeYouTubeApp(url, opts = {}) {
  const key = youtubeVideoId(url);
  const cached = youtubeProbeCache.get(key);
  if (cached && Date.now() - cached.time < 90000) return cached.promise;
  const promise = probeApp(url, opts).then(result => {
    if (!result?.ok || !result.formats?.length) youtubeProbeCache.delete(key);
    return result;
  });
  youtubeProbeCache.set(key, {time:Date.now(), promise});
  return promise;
}

async function previewDestination(item) {
  try {
    const res = await fetch(`${await apiBase()}/api/route`, {
      method: "POST",
      headers: await authHeaders(),
      body: JSON.stringify({
        url: item.url,
        page_url: item.pageUrl || "",
        filename: filenameForMedia(item, {}),
        media_type: item.kind === "file" ? "http" : item.kind,
      }),
    });
    return await res.json();
  } catch {
    return { ok: false };
  }
}

function readableError(value) {
  const error = String(value || '未知错误');
  if (/cloudflare|403 forbidden|http error 403/i.test(error))
    return '网站拒绝了网页解析；如果已识别到播放器视频，请使用推荐视频下载。';
  if (/sign in to confirm|not a bot/i.test(error))
    return '网站要求登录或人机验证，请在浏览器完成验证后重试。';
  if (/unsupported url/i.test(error)) return '当前网页地址暂不支持解析，请播放后使用浏览器识别的视频。';
  if (/the page needs to be reloaded/i.test(error)) return '请刷新网页并播放视频后重试。';
  return error;
}

async function collectCookies(url) {
  try {
    // `cookies` is an OPTIONAL permission the user grants explicitly (a toggle
    // in the popup). Until then we read nothing and send no cookies — public
    // downloads still work; only login-gated ones need the opt-in.
    const granted = await B.permissions.contains({ permissions: ["cookies"] });
    if (!granted) return "";
    const cookies = await B.cookies.getAll({ url });
    if (!cookies?.length) return "";
    return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch (_) {
    return "";
  }
}

async function sendToApp(url, opts = {}) {
  console.info("[拾流链路] preparing_request", { mediaType: opts.media_type || "http" });
  const cfg = await getConfig();
  console.info("[拾流链路] extension_config", { enabled: cfg.enabled, port: cfg.port, tokenConfigured: !!cfg.token });
  if (!cfg.enabled) {
    notify("拾流下载器", "扩展已关闭，请在扩展面板中启用。");
    return { ok: false, error: "disabled" };
  }

  const cookie = opts.cookie ?? (await collectCookies(url));
  const payload = {
    url,
    filename: opts.filename || guessName(url),
    referrer: opts.referrer || "",
    page_url: opts.pageUrl || opts.referrer || "",
    title: opts.title || "",
    media_type: opts.media_type || "http",
    user_agent: navigator.userAgent || "",
    cookie,
    start: true,
  };
  if (opts.height) payload.height = opts.height;
  if (opts.format_id) payload.format_id = opts.format_id;
  if (opts.audio_only) payload.audio_only = true;
  if (opts.best) payload.best = true;
  if (opts.browser_player_url) payload.browser_player_url = opts.browser_player_url;
  if (opts.browser_pair) payload.browser_pair = opts.browser_pair;

  try {
    const base = await apiBase();
    console.info("[拾流链路] request_sent", { endpoint: `${base}/api/add` });
    const res = await fetch(`${base}/api/add`, {
      method: "POST",
      headers: await authHeaders(),
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      console.info("[拾流链路] http_response", { status: res.status, ok: data.ok, error: data.error });
      const err = readableError(data.error || `HTTP ${res.status}`);
      notify("拾流下载器", `失败：${err}。请检查桌面软件是否运行。`, "md-add-failed");
      return { ok: false, error: err };
    }
    console.info("[拾流链路] http_response", { status: res.status, ok: data.ok, jobId: data.id, prompted: !!data.prompted });
    if (data.prompted) {
      notify("拾流下载器", `请在桌面软件中选择下载设置：${data.filename || payload.filename}`);
    } else {
      const label = data.media_type && data.media_type !== "http" ? "（视频）" : "";
      notify("拾流下载器", `已加入队列${label}：${data.filename || payload.filename}`);
    }
    return { ok: true, ...data };
  } catch (e) {
    console.error("[拾流链路] request_failed", String(e.message || e));
    notifyOffline();   // throttled, single reusable toast — never a storm
    return { ok: false, error: String(e.message || e) };
  }
}

// ── helpers ──────────────────────────────────────────────────────────────────

function guessName(url) {
  try {
    const u = new URL(url);
    const last = u.pathname.split("/").filter(Boolean).pop() || "download";
    return decodeURIComponent(last.split("?")[0]) || "download";
  } catch (_) {
    return "download";
  }
}

function basename(path) {
  if (!path) return "";
  const parts = path.replace(/\\/g, "/").split("/");
  return parts[parts.length - 1] || "";
}

// Passing an id makes chrome REPLACE any existing notification with that id
// instead of stacking a new toast. Without one, every call spawns a fresh
// notification, and the OS drip-feeds a burst of them out one at a time — which
// is exactly the flickering "Cannot reach app" storm when the app is off.
function notify(title, message, id) {
  try {
    const opts = {
      type: "basic",
      iconUrl: "icons/icon128.png",
      title,
      message,
      silent: true, // no notification sound/beep
    };
    if (id) chrome.notifications.create(id, opts);
    else chrome.notifications.create(opts);
  } catch (_) {
    /* notifications may be blocked */
  }
}

// The "app isn't running" notice. One reusable toast (fixed id), shown at most
// once every OFFLINE_NOTICE_MS — so a page firing several downloads, or any
// repeated failure, can never turn into a wall of toasts.
const OFFLINE_NOTICE_ID = "md-app-offline";
const OFFLINE_NOTICE_MS = 10000;
let lastOfflineNotice = 0;
async function notifyOffline() {
  const now = Date.now();
  if (now - lastOfflineNotice < OFFLINE_NOTICE_MS) return;
  lastOfflineNotice = now;
  // getConfig() rather than a bare `cfg`: this is a top-level helper with no
  // config in scope, and the port is user-configurable.
  const port = (await getConfig()).port;
  notify(
    "拾流下载器",
    "无法连接桌面软件。请启动 拾流下载器（端口 " + port + "）。",
    OFFLINE_NOTICE_ID
  );
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}
