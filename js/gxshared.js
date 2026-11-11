import { app } from "../../../scripts/app.js";
import { gxFetch, gxConfig, onGxSettingsChanged } from "./gxapi.js";
import { gxText } from "./gxi18n.js";

const KEY = "gxnodes.shared.compute.order";
const LOCK_KEY = "gxnodes.shared.compute.locked";
const RESOURCE_CONTRACT_VERSION = 2;
const parseOrder = (raw) => {
  try {
    const parsed = JSON.parse(raw || "[]");
    return Array.isArray(parsed) ? parsed.filter((id) => typeof id === "string" && id).slice(0, 512) : [];
  } catch (_) { return []; }
};
const parseLocks = (raw) => {
  if (raw === null || raw === undefined) return [];
  if (typeof raw !== "string" || raw.length > 64 * 1024) return null;
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed) || parsed.length > 512 ||
      !parsed.every((id) => typeof id === "string" && id && id.length <= 128)) return null;
    return [...new Set(parsed)];
  } catch (_) { return null; }
};
const initialOrder = (() => { try { return parseOrder(localStorage.getItem(KEY)); } catch (_) { return []; } })();
const initialLocks = (() => { try { return parseLocks(localStorage.getItem(LOCK_KEY)); } catch (_) { return null; } })();
const state = { peers: [], jobs: [], jobsUnavailable: false, activeTab: "compute", jobFilter: "all", order: initialOrder, locked: initialLocks === null ? [] : initialLocks, lockUnavailable: initialLocks === null, selectedId: null, discoveryError: false, loadCount: 0, settings: {}, signature: "" };
const TAB_ID = "gx-shared-compute";
let sharedEnabled = true;
let tabRegistered = false;
let lockSyncInstalled = false;
let lockNeedsRepair = false;
let pollTimer = null;
let pushInFlight = false;
let incomingSaving = false;
let incomingUncertain = false;
let incomingVersion = 0;
let refreshVersion = 0;
let jobReceiptVersion = 0;
let activeRefresh = null;
const formatGiB = (value) => `${(value / 1024 ** 3).toFixed(1)} GB`;
const saveOrder = () => localStorage.setItem(KEY, JSON.stringify(state.order));
const isLocked = (peer) => state.locked.includes(peer.id);
const performanceScore = (peer) => (peer.hardware?.vram_total || 0) * 2 + (peer.hardware?.vram_free || 0) - (peer.queue || 0) * 1024 ** 3;
const setSelection = (peerId) => { state.selectedId = peerId; render(); updateRunButton(); };
const peersByPriority = () => [...state.peers].sort((a, b) => {
  const ai = state.order.indexOf(a.id), bi = state.order.indexOf(b.id);
  if (ai < 0 && bi < 0) return performanceScore(b) - performanceScore(a);
  return (ai < 0 ? Number.MAX_SAFE_INTEGER : ai) - (bi < 0 ? Number.MAX_SAFE_INTEGER : bi);
});
const canSelectPeer = (peer) => !state.lockUnavailable && !isLocked(peer) && peer.allow_incoming_shared !== false;
// An explicit selection must never silently move to a different instance.
// Stale discovery data or unreadable lock state must not authorise a push.
const selectedPeer = () => state.discoveryError || state.lockUnavailable
  ? null : state.peers.find((peer) => peer.id === state.selectedId && canSelectPeer(peer)) || null;

const syncLocks = (locked, unavailable = false) => {
  state.locked = locked;
  state.lockUnavailable = unavailable;
  state.signature = "";
  try { render(); } catch (_) { /* Keep the button state authoritative. */ }
  finally { updateRunButton(); }
};
function readLockProjection(fromRemote = false) {
  if (fromRemote) lockNeedsRepair = false;
  if (lockNeedsRepair) { syncLocks([], true); return; }
  let locked = [], unavailable = false;
  try {
    locked = parseLocks(localStorage.getItem(LOCK_KEY));
    if (locked === null) { locked = []; unavailable = true; lockNeedsRepair = true; }
  } catch (_) { locked = []; unavailable = true; lockNeedsRepair = true; }
  syncLocks(locked, unavailable);
}
function installLockSync() {
  if (lockSyncInstalled) return;
  lockSyncInstalled = true;
  if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
    window.addEventListener("storage", (event) => {
      if (!event || (event.storageArea && event.storageArea !== localStorage)) return;
      if (event.key === LOCK_KEY || event.key === null) readLockProjection(true);
    });
    window.addEventListener("pageshow", () => readLockProjection());
  }
  if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") readLockProjection(); });
  }
  readLockProjection();
}
async function withLock(commit) {
  try {
    const lockManager = typeof navigator !== "undefined" ? navigator.locks : null;
    if (lockManager && typeof lockManager.request === "function") await lockManager.request(LOCK_KEY, commit);
    else commit();
    return true;
  } catch (_) {
    lockNeedsRepair = true;
    syncLocks([], true);
    showNotice(gxText("排除设置未保存", "Exclusion not saved"), gxText("检查浏览器站点存储。", "Check browser site storage."), true);
    return false;
  }
}
async function setLock(peerId, desired) {
  if (typeof peerId !== "string" || !peerId || peerId.length > 128 || typeof desired !== "boolean") return false;
  return withLock(() => {
    const current = parseLocks(localStorage.getItem(LOCK_KEY));
    if (current === null) throw new Error("lock state unavailable");
    const next = desired ? (current.includes(peerId) ? current : [...current, peerId]) : current.filter((id) => id !== peerId);
    localStorage.setItem(LOCK_KEY, JSON.stringify(next));
    lockNeedsRepair = false;
    syncLocks(next, false);
  });
}
async function resetLockState() {
  return withLock(() => {
    localStorage.setItem(LOCK_KEY, "[]");
    lockNeedsRepair = false;
    syncLocks([], false);
  });
}

function ensureStyles() {
  if (document.querySelector("#gx-shared-compute-style")) return;
  const style = document.createElement("style"); style.id = "gx-shared-compute-style";
  style.textContent = `
    #gx-shared-compute{
      color-scheme:dark;
      --gx-panel:light-dark(#f6f8fb,#171a1f);
      --gx-card:light-dark(#fff,#1e2229);
      --gx-line:light-dark(#cbd3df,#333944);
      --gx-text:light-dark(#202734,#eef1f6);
      --gx-muted:light-dark(#505c6d,#a4adbb);
      --gx-accent:light-dark(#2459a5,#87b7ff);
      --gx-selected:light-dark(#e5efff,#212b3c);
      --gx-filter-selected:light-dark(#e5efff,#293447);
      --gx-button:light-dark(#2859a0,#a0c6ff);
      --gx-button-text:light-dark(#fff,#111f33);
      --gx-button-disabled:light-dark(#e3e7ed,#333b47);
      --gx-button-disabled-text:light-dark(#505c6d,#b6c1d0);
      --gx-success:light-dark(#17694d,#78d6b1);
      --gx-warning:light-dark(#8b460e,#f1b479);
      --gx-error:light-dark(#a4362c,#f5a69b);
    }
    html:not(.dark-theme) #gx-shared-compute{color-scheme:light}
    #gx-shared-compute{color:var(--gx-text);background:var(--gx-panel);font:13px/1.45 var(--font-family,system-ui,sans-serif)}
    .gx-compute-panel{display:grid;gap:12px;padding:16px;min-width:0}
    .gx-compute-heading{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
    .gx-compute-title{margin:0;font-size:16px;font-weight:650}
    .gx-compute-count,.gx-incoming-status,.gx-peer-state{display:inline-flex;align-items:center;gap:5px;color:var(--gx-muted);font-size:11px}
    .gx-incoming{display:flex;align-items:center;gap:8px;padding:7px 0}
    .gx-incoming-label{flex:1;font-weight:600;cursor:pointer}
    .gx-incoming input,.gx-lock-control input{margin:0;accent-color:var(--gx-accent)}
    #gx-incoming-shared{appearance:none;position:relative;width:27px;height:15px;border:0;border-radius:99px;background:light-dark(#757f8c,#454e59);cursor:pointer}
    #gx-incoming-shared::before{content:"";position:absolute;top:3px;left:3px;width:9px;height:9px;border-radius:50%;background:#e9eef5}
    #gx-incoming-shared:checked{background:light-dark(#17694d,#317e68)}
    #gx-incoming-shared:checked::before{transform:translateX(12px)}
    #gx-incoming-shared:indeterminate::before{transform:translateX(6px)}
    #gx-incoming-shared:disabled{opacity:.65;cursor:default}
    .gx-incoming-details{font-size:11px;color:var(--gx-muted)}
    .gx-incoming-details summary{width:fit-content;cursor:pointer}
    .gx-incoming-details p{margin:6px 0 0;line-height:1.55}
    .gx-compute-list{display:flex;flex-direction:column;gap:8px}
    .gx-compute-row{display:grid;gap:6px;padding:10px;border:1px solid var(--gx-line);border-radius:8px;background:var(--gx-card);cursor:pointer;transition:border-color .12s ease,opacity .12s ease}
    .gx-compute-row:hover{border-color:color-mix(in srgb,var(--gx-accent) 40%,var(--gx-line));background:color-mix(in srgb,var(--gx-card) 80%,var(--gx-selected))}
    .gx-compute-row.is-selected{border-color:var(--gx-accent);background:var(--gx-selected)}
    .gx-compute-row.is-locked{opacity:.8;cursor:default}
    .gx-compute-row.is-over{border-color:var(--gx-accent);background:color-mix(in srgb,var(--gx-accent) 10%,var(--gx-card))}
    .gx-compute-row.is-dragging{opacity:.45}
    .gx-compute-name{display:flex;align-items:center;justify-content:space-between;gap:6px;min-width:0}
    .gx-peer-select{min-width:0;padding:0;border:0;background:transparent;color:inherit;font:inherit;font-weight:650;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}
    .gx-peer-select:disabled{cursor:default}
    .gx-peer-state{white-space:nowrap}
    .gx-compute-meta{margin-top:3px;color:var(--gx-muted);font-size:11px;overflow-wrap:anywhere}
    .gx-compute-metrics{display:flex;flex-wrap:wrap;gap:3px 10px;color:var(--gx-muted);font-size:11px;font-variant-numeric:tabular-nums}
    .gx-compute-actions{display:flex;align-items:center;justify-content:space-between;gap:6px;margin-top:6px}
    .gx-peer-options,.gx-task-details{color:var(--gx-muted);font-size:11px}
    .gx-peer-options summary,.gx-task-details summary{width:fit-content;cursor:pointer}
    .gx-lock-control{display:flex;align-items:center;gap:5px;padding:0 2px;color:var(--gx-muted);font-size:11px;user-select:none}
    .gx-compute-order{display:flex;align-items:center;gap:4px}
    .gx-compute-icon{min-width:24px;min-height:24px;border:1px solid var(--gx-line);border-radius:5px;color:var(--gx-text);background:transparent;cursor:pointer;font-size:12px}
    .gx-compute-icon:hover:not(:disabled){color:var(--gx-text);border-color:var(--gx-accent)}
    .gx-compute-icon:disabled{opacity:.35;cursor:default}
    .gx-compute-empty,.gx-results-empty{display:grid;justify-items:center;gap:8px;padding:20px 12px;border:1px dashed var(--gx-line);border-radius:8px;color:var(--gx-muted);text-align:center}
    .gx-run-controls{display:grid;gap:6px}
    .gx-run-target{color:var(--gx-muted);font-size:11px;overflow-wrap:anywhere}
    #gx-shared-run{min-height:34px;border:1px solid transparent;border-radius:7px;padding:6px 12px;font:inherit;font-weight:650;color:var(--gx-button-text);background:var(--gx-button);cursor:pointer}
    #gx-shared-run:hover:not(:disabled){filter:brightness(1.12)}
    #gx-shared-run:disabled{background:var(--gx-button-disabled);color:var(--gx-button-disabled-text);cursor:default}
    .gx-job-result{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:6px;color:var(--gx-muted);overflow-wrap:anywhere}
    .gx-job-load{flex:none;min-height:26px;padding:0 8px;border:1px solid var(--gx-line);border-radius:5px;color:var(--gx-text);background:transparent;cursor:pointer;font-size:11px}
    .gx-job-load:hover{border-color:var(--gx-accent)}
    .gx-shared-tabs{display:flex;gap:2px;border-bottom:1px solid var(--gx-line)}
    .gx-shared-tab{flex:1;padding:7px;border:0;border-bottom:2px solid transparent;color:var(--gx-muted);background:transparent;cursor:pointer;font:inherit}
    .gx-shared-tab.is-active{color:var(--gx-text);border-bottom-color:var(--gx-accent);font-weight:650}
    #gx-shared-compute :is(button,input,summary):focus-visible{outline:2px solid var(--gx-accent);outline-offset:2px}
    .gx-shared-preview-host{width:100%;min-height:88px}
    .gx-shared-preview{display:flex;align-items:center;justify-content:center;width:100%;min-height:88px;border:1px solid var(--border-color,#3d3d3d);border-radius:6px;background:#111;overflow:hidden}
    .gx-shared-preview-empty{padding:14px;color:var(--descrip-text,#9a9a9a);font-size:12px}
    .gx-shared-preview-media{display:block;width:100%;height:auto;max-width:100%;max-height:none;object-fit:contain}
    .gx-shared-preview-audio{display:block;width:100%}
    .gx-result-card{padding:10px;border:1px solid var(--gx-line);border-radius:8px;background:var(--gx-card)}
    .gx-result-header{display:flex;justify-content:space-between;align-items:start;gap:8px;font-size:12px}
    .gx-result-header b{min-width:0;overflow-wrap:anywhere}
    .gx-job-filters{display:flex;flex-wrap:wrap;gap:5px}
    .gx-job-filter{padding:4px 7px;border:1px solid var(--gx-line);border-radius:6px;color:var(--gx-muted);background:transparent;font:inherit;font-size:11px;cursor:pointer}
    .gx-job-filter[aria-pressed=true]{color:var(--gx-text);border-color:var(--gx-accent);background:var(--gx-filter-selected)}
    .gx-peer-state::before{content:"";width:6px;height:6px;flex:none;border-radius:50%;background:currentColor}
    .gx-job-status-active{color:var(--gx-warning)}
    .gx-job-status-done,.gx-peer-ready{color:var(--gx-success)}
    .gx-job-status-attention{color:var(--gx-error)}
    .gx-task-details{margin-top:6px}
    .gx-task-note{margin-top:5px;color:var(--gx-muted);font-size:11px;overflow-wrap:anywhere}
    .gx-result-media{display:block;width:100%;max-height:210px;margin-top:9px;object-fit:contain;border:1px solid var(--gx-line);border-radius:4px;background:#111}
    .gx-result-audio{display:block;width:100%;margin-top:9px}
  `;
  document.head.appendChild(style);
}

function showNotice(title, detail = "", error = false) {
  const toast = app.extensionManager?.toast;
  const severity = error ? "error" : "info";
  const life = error ? 7000 : 4000;
  if (toast?.[severity]) toast[severity](title, { detail, life });
  else toast?.add?.({ severity, summary: title, detail, life });
}

function mediaKindForPath(path) {
  if (IMAGE_PATTERN.test(path)) return { type: "GXSharedLoadImage", kind: "image" };
  if (VIDEO_PATTERN.test(path)) return { type: "GXSharedLoadVideo", kind: "video" };
  if (AUDIO_PATTERN.test(path)) return { type: "GXSharedLoadAudio", kind: "audio" };
  return null;
}

function makePreviewElement(kind) {
  const wrapper = document.createElement("div");
  wrapper.className = "gx-shared-preview";
  const placeholder = document.createElement("div");
  placeholder.className = "gx-shared-preview-empty";
  placeholder.textContent = gxText("等待共享结果", "Waiting for result");
  wrapper.appendChild(placeholder);

  const update = (url, label, onMediaReady) => {
    wrapper.replaceChildren();
    if (!url) {
      placeholder.textContent = label || gxText("等待共享结果", "Waiting for result");
      wrapper.appendChild(placeholder);
      return;
    }
    if (kind === "image") {
      const image = document.createElement("img");
      image.className = "gx-shared-preview-media";
      image.loading = "lazy";
      image.alt = label || gxText("共享图片", "Shared image");
      image.src = url;
      image.addEventListener("load", () => onMediaReady?.(image.naturalWidth, image.naturalHeight), { once: true });
      wrapper.appendChild(image);
    } else if (kind === "video") {
      const video = document.createElement("video");
      video.className = "gx-shared-preview-media";
      video.controls = true;
      video.preload = "metadata";
      video.src = url;
      video.addEventListener("loadedmetadata", () => onMediaReady?.(video.videoWidth, video.videoHeight), { once: true });
      wrapper.appendChild(video);
    } else if (kind === "audio") {
      const audio = document.createElement("audio");
      audio.className = "gx-shared-preview-audio";
      audio.controls = true;
      audio.preload = "metadata";
      audio.src = url;
      wrapper.appendChild(audio);
    }
  };

  return { wrapper, update };
}

function syncSharedPreviewSize(node, width = node.size?.[0]) {
  const widget = node._gxSharedPreviewWidget;
  const host = node._gxSharedPreviewHost;
  if (!widget || !host || node._gxSharedSizing) return;

  const nextWidth = Math.max(320, Math.round(width || 320));
  const previewHeight = widget.computeSize(nextWidth)[1];
  host.style.width = `${Math.max(1, nextWidth - 20)}px`;
  host.style.height = `${previewHeight}px`;

  // Keep the node chrome stable while replacing only the preview band's height.
  const nextHeight = Math.ceil((node._gxSharedBaseHeight || 0) + previewHeight);
  node._gxSharedSizing = true;
  node.setSize?.([nextWidth, nextHeight]);
  node._gxSharedSizing = false;
  node.setDirtyCanvas?.(true, true);
}

function ensureSharedPreview(node, kind) {
  if (node._gxSharedPreviewWidget) return node._gxSharedPreviewWidget;
  node._gxSharedBaseHeight = Math.ceil(node.computeSize?.()?.[1] || 0);
  const host = document.createElement("div");
  host.className = "gx-shared-preview-host";
  const { wrapper, update } = makePreviewElement(kind);
  host.appendChild(wrapper);
  const widget = node.addDOMWidget("preview", "gxshared_preview", host, {
    serialize: false,
    getValue() { return ""; },
    setValue() {},
  });
  widget.element = host;
  node._gxSharedAspect = null;
  widget.computeSize = (width) => {
    const w = Math.max(260, width || 260);
    if (kind === "audio") return [w, 88];
    const aspect = node._gxSharedAspect || 16 / 9;
    return [w, Math.max(120, Math.round((w - 20) / aspect) + 10)];
  };
  const originalOnResize = node.onResize;
  node.onResize = function(size) {
    originalOnResize?.apply(this, arguments);
    if (node._gxSharedSizing) return;
    requestAnimationFrame(() => syncSharedPreviewSize(node, size?.[0]));
  };
  node._gxSharedPreviewWidget = widget;
  node._gxSharedPreviewHost = host;
  node._gxSharedPreviewUpdate = update;
  node._gxSharedRefreshPreview = () => refreshSharedPreview(node);
  requestAnimationFrame(() => {
    refreshSharedPreview(node);
    syncSharedPreviewSize(node);
  });
  return widget;
}

function refreshSharedPreview(node) {
  const widget = node._gxSharedPreviewWidget;
  const updater = node._gxSharedPreviewUpdate;
  if (!widget || !updater) return;
  const result = node.widgets?.find((item) => item.name === "result")?.value || "";
  const preview = outputViewUrl(result);
  const label = String(result || "").split(/[\\/]/).pop();
  updater(preview, label, (width, height) => {
    if (width > 0 && height > 0) {
      node._gxSharedAspect = width / height;
      syncSharedPreviewSize(node);
    }
  });
  syncSharedPreviewSize(node);
}

async function refresh() {
  // The 3-second timer is a trigger, not permission to supersede an unfinished
  // batch. Otherwise every timeout can become stale before it disables peers.
  if (!sharedEnabled || activeRefresh) return;
  const controller = new AbortController();
  activeRefresh = controller;
  const deadline = setTimeout(() => controller.abort(), 10000);
  const refreshId = ++refreshVersion;
  const settingsVersion = incomingVersion;
  const receiptVersion = jobReceiptVersion;
  const current = () => refreshId === refreshVersion && sharedEnabled;
  const settledJson = async (url) => {
    try {
      const response = await gxFetch(url, { signal: controller.signal });
      if (!response || response.ok !== true) return null;
      return await response.json();
    } catch (_) { return null; }
  };
  const validPeers = (data) => {
    const peers = data?.peers;
    if (!Array.isArray(peers) || !peers.every((peer) => peer && typeof peer === "object" && !Array.isArray(peer)
      && typeof peer.id === "string" && peer.id.length > 0 && peer.id.length <= 128
      && typeof peer.host === "string" && peer.host.length > 0
      && Number.isInteger(peer.port) && peer.port > 0 && peer.port < 65536
      && (peer.allow_incoming_shared === undefined || peer.allow_incoming_shared === null || typeof peer.allow_incoming_shared === "boolean"))) return null;
    return peers;
  };
  const validJobs = (data) => {
    const jobs = data?.jobs;
    if (!Array.isArray(jobs) || !jobs.every((job) => job && typeof job === "object" && !Array.isArray(job)
      && typeof job.id === "string" && job.id.length > 0
      && (job.outputs === undefined || job.outputs === null || Array.isArray(job.outputs)))) return null;
    return jobs;
  };
  const commit = () => {
    try {
      const content = state.activeTab === "results" ? [state.jobs, state.jobFilter, state.jobsUnavailable] : [
        state.peers.map(({ last_seen, ...peer }) => peer), state.selectedId, state.discoveryError, state.lockUnavailable,
        state.settings.allow_incoming_shared, state.settings.incoming_shared_available,
      ];
      const signature = JSON.stringify([state.activeTab, content]);
      if (signature !== state.signature && render() !== false) state.signature = signature;
      const badge = document.querySelector("#gx-shared-compute .gx-compute-count");
      if (badge) badge.textContent = peerCountLabel();
    } catch (_) { /* A view-layer failure must not leave the run button enabled. */ }
    finally { updateRunButton(); }
  };
  const guard = (apply) => (data) => {
    try { if (current()) { apply(data); commit(); } } catch (_) { /* Keep polling alive after a view-layer failure. */ }
  };
  const tasks = [
    settledJson("/gx/shared/peers").then(guard((peersData) => {
      const discovered = validPeers(peersData);
      state.discoveryError = discovered === null;
      state.peers = discovered || [];
    })),
    settledJson("/gx/shared/jobs").then(guard((jobsData) => {
      if (receiptVersion !== jobReceiptVersion) return;
      const jobs = validJobs(jobsData);
      state.jobsUnavailable = jobs === null;
      if (jobs !== null) state.jobs = jobs;
    })),
    settledJson("/gx/settings").then(guard((settingsData) => {
      if (!incomingSaving && !incomingUncertain && settingsVersion === incomingVersion) {
        const settings = settingsData?.settings;
        if (settings && typeof settings === "object" && !Array.isArray(settings)) state.settings = settings;
        else state.settings = { ...state.settings, allow_incoming_shared: undefined, incoming_shared_available: undefined };
      }
    })),
  ];
  try { await Promise.allSettled(tasks); }
  finally {
    clearTimeout(deadline);
    if (activeRefresh === controller) activeRefresh = null;
  }
}

async function postJson(url, payload) {
  const response = await gxFetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }).catch((error) => {
    if (url === "/gx/shared/push") throw submissionUnknown();
    throw error;
  });
  const body = await response.json().catch(() => ({}));
  if (body?.code === "submission_unknown") throw submissionUnknown();
  if (body?.code === "accepted_save_unconfirmed") throw new Error(
    gxText("目标已接收。请核对目标队列，勿重复提交。", "The target accepted this job. Check its queue before retrying."),
    { cause: "accepted_save_unconfirmed" },
  );
  if (url === "/gx/shared/push" && response.status >= 500 && body?.ok !== false) throw submissionUnknown();
  if (!response.ok) throw new Error(body?.error || gxText(`请求失败 HTTP ${response.status}`, `Request failed (HTTP ${response.status})`));
  return body;
}

const submissionUnknown = () => new Error(
  gxText("请先核对目标队列，勿重复提交。", "Check the target queue before retrying."),
  { cause: "submission_unknown" },
);
const formatBytes = (value) => value < 1024 ** 2 ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1024 ** 2).toFixed(1)} MB`;

async function confirmResources(target, preparation) {
  const blocked = Array.isArray(preparation?.blocked) ? preparation.blocked : [];
  const resources = Array.isArray(preparation?.resources) ? preparation.resources : [];
  const resourceLabel = blocked.length ? gxText("已列出资源（可能不全）", "Listed files (may be incomplete)") : gxText("资源", "Files");
  const size = formatBytes(preparation?.total_size || 0);
  const lines = [gxText(`目标：${target.name || target.host}:${target.port}`, `Target: ${target.name || target.host}:${target.port}`), `${resourceLabel}: ${resources.length} · ${size}`];
  const quoted = (value) => JSON.stringify(String(value).slice(0, 240)).replace(/[\u007f-\u009f\u061c\u200e\u200f\u2028-\u202e\u2066-\u2069]/g, (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`);
  resources.forEach((resource) => {
    const pointer = resource.pointer;
    const input = Array.isArray(pointer) && pointer[1] === "inputs" && typeof pointer[2] === "string"
      ? `${quoted(pointer[2])}${pointer.length > 3 ? gxText("（嵌套）", " (nested)") : ""}` : gxText("字段未知", "Unknown field");
    const file = quoted(resource.relative_path || resource.filename || gxText("未知资源", "Unknown file"));
    const node = quoted(resource.node_id ?? "?");
    lines.push(`${file} · ${formatBytes(resource.size)} · ${gxText(`节点 ${node} · 输入 ${input}`, `Node ${node} · Input ${input}`)}`);
  });
  if (resources.length) {
    lines.push(gxText("请核对节点和输入。仅传输已支持的单文件素材，清单中的输入将替换为远端路径。未知字段匹配本地文件时会阻止发送。", "Review the nodes and inputs. Only supported single-file inputs transfer, and listed inputs will use remote paths. A local file in an unknown field blocks sending."));
    lines.push(gxText("发送前为全部素材创建并校验临时快照。与清单不符时停止发送；传输使用已确认的内容。", "All files are snapshotted and checked before sending. A mismatch stops the transfer; only approved content is sent."));
  }
  const preflight = preparation?.preflight;
  const nodes = Array.isArray(preflight?.missing_nodes) ? preflight.missing_nodes : [];
  const models = Array.isArray(preflight?.missing_models) ? preflight.missing_models : [];
  for (const item of nodes.slice(0, 12)) {
    if (typeof item?.class_type === "string") lines.push(`${gxText("缺少节点或定义", "Missing node or definition")}: ${quoted(item.class_type)} (#${quoted(item.node_id ?? "?")})`);
  }
  for (const item of models.slice(0, 12)) {
    if (typeof item?.value === "string") lines.push(`${gxText("缺少选项", "Missing option")}: ${quoted(item.value)} (#${quoted(item.node_id ?? "?")}, ${quoted(item.input ?? "?")})`);
  }
  if (nodes.length > 12 || models.length > 12) lines.push(gxText("另有未列出的预检问题。", "More preflight issues are not listed."));
  if (blocked.length) lines.push(...blocked.map((item) => `${gxText("阻止", "Blocked")}: ${item.error || gxText("目标预检未通过", "Preflight failed")}`));
  const message = lines.join("\n");
  if (preparation?.ok !== true) {
    if (app.extensionManager?.dialog?.confirm) await app.extensionManager.dialog.confirm({ type: "info", title: gxText("无法发送任务", "Cannot send job"), message });
    else showNotice(gxText("无法发送任务", "Cannot send job"), message, true);
    return false;
  }
  if (app.extensionManager?.dialog?.confirm) return app.extensionManager.dialog.confirm({ title: gxText("确认共享任务", "Confirm shared job"), message });
  return window.confirm(message);
}

async function push(target, isTargetCurrent = () => true) {
  const targetUrl = `http://${target.host}:${target.port}`;
  const name = target.name || target.host;
  const workflowStore = app.extensionManager?.workflow;
  const workflow = workflowStore?.activeWorkflow;
  const workflowName = workflow?.filename;
  const graphId = workflow?.activeState?.id;
  const checkWorkflow = () => {
    if (app.configuringGraph || workflowStore?.activeWorkflow !== workflow) {
      throw new Error(gxText("工作流已切换，请重新发起共享运行。", "The active workflow changed. Start the shared run again."));
    }
  };
  const checkTarget = () => {
    if (!isTargetCurrent()) throw new Error(gxText("目标状态已变化，请重新选择实例。", "The target changed. Choose an instance again."));
  };
  const message = gxText(
    `目标：${name}\n地址：${targetUrl}\n预检会发送工作流（含提示词），后续可能上传文件素材，并附带已配置的共享令牌。已发送内容不能撤回。\n本次共享使用明文 HTTP，局域网发现未验证目标身份。请核对地址，仅在可信网络使用。共享令牌只保护部分 GX 接口，不保护 ComfyUI 原生接口。`,
    `Target: ${name}\nAddress: ${targetUrl}\nPreflight sends the workflow, including prompts. Files and any configured sharing token may follow. Sent data cannot be recalled.\nTraffic uses unencrypted HTTP and discovery does not verify the target. Verify the address on a trusted network. The token only protects some GX endpoints; native ComfyUI endpoints are outside its scope.`,
  );
  const confirmed = app.extensionManager?.dialog?.confirm
    ? await app.extensionManager.dialog.confirm({ title: gxText("确认共享目标", "Confirm target"), message })
    : window.confirm(message);
  if (!confirmed) return null;
  checkWorkflow();
  const snapshot = await app.graphToPrompt?.();
  checkWorkflow();
  const serializedId = snapshot?.workflow?.id;
  if (graphId && serializedId && serializedId !== graphId && serializedId !== workflow?.legacyId) {
    throw new Error(gxText("工作流已切换，请重新发起共享运行。", "The active workflow changed. Start the shared run again."));
  }
  if (!snapshot?.output || !Object.keys(snapshot.output).length) {
    throw new Error(gxText("工作流没有可运行的节点。", "The workflow has no runnable nodes."));
  }
  // Custom widgets can return a mutable value. Keep both POSTs on the same JSON snapshot.
  const prompt = JSON.parse(JSON.stringify(snapshot.output));
  checkTarget();
  const preparation = await postJson("/gx/shared/prepare", { target: targetUrl, prompt });
  if (!await confirmResources(target, preparation)) return null;
  if (!Array.isArray(preparation.resources)) throw new Error(gxText("资源确认结果无效", "Invalid resource confirmation"));
  checkTarget();
  const receipt = await postJson("/gx/shared/push", { target: targetUrl, prompt, workflow_name: workflowName, resources: preparation.resources, resource_contract: RESOURCE_CONTRACT_VERSION });
  if (receipt?.ok !== true) throw submissionUnknown();
  return receipt;
}

function move(peerId, direction) { const peers = peersByPriority(), index = peers.findIndex((peer) => peer.id === peerId); if (index < 0 || !peers[index + direction]) return; [peers[index], peers[index + direction]] = [peers[index + direction], peers[index]]; state.order = peers.map((peer) => peer.id); saveOrder(); render(); updateRunButton(); }
function reorder(fromId, toId) {
  if (!fromId || !toId || fromId === toId) return;
  const peers = peersByPriority();
  const fromIndex = peers.findIndex((peer) => peer.id === fromId);
  const toIndex = peers.findIndex((peer) => peer.id === toId);
  if (fromIndex < 0 || toIndex < 0) return;
  const [moved] = peers.splice(fromIndex, 1);
  peers.splice(toIndex, 0, moved);
  state.order = peers.map((peer) => peer.id);
  saveOrder();
  render();
  updateRunButton();
}
async function setIncomingShared(enabled, control, status) {
  if (incomingSaving || incomingUncertain) return;
  incomingSaving = true;
  incomingVersion++;
  control.disabled = true;
  status.textContent = gxText("保存中", "Saving");
  let errorCode = "";
  let confirmedRejection = false;
  try {
    const response = await gxFetch("/gx/settings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allow_incoming_shared: enabled }),
    });
    const body = await response.json();
    errorCode = body?.code || "";
    confirmedRejection = (response.status === 403 && errorCode === "gx_local_only") ||
      (response.status === 409 && errorCode === "gx_settings_busy") ||
      (response.status === 503 && ["gx_incoming_unavailable", "gx_security_unavailable"].includes(errorCode));
    if (!response.ok || body?.ok === false || body.settings?.allow_incoming_shared !== enabled || body.settings?.incoming_shared_available !== true) throw new Error("Save unconfirmed");
    state.settings = body.settings;
    status.textContent = enabled ? gxText("开启", "On") : gxText("已暂停", "Paused");
  } catch (_) {
    state.settings.allow_incoming_shared = undefined;
    state.settings.incoming_shared_available = undefined;
    control.indeterminate = true;
    status.textContent = gxText("正在核对", "Checking");
    if (!confirmedRejection) {
      incomingUncertain = true;
      status.textContent = gxText("状态未知", "Unknown");
      showNotice(gxText("接收状态未知", "Receive status unknown"), gxText("刷新后核对。", "Refresh to check."), true);
    } else try {
      const response = await gxFetch("/gx/settings", { cache: "no-store" });
      const body = await response.json();
      if (!response.ok || typeof body?.settings?.allow_incoming_shared !== "boolean") throw new Error("Receive state unavailable");
      state.settings = body.settings;
      const actual = state.settings.allow_incoming_shared;
      const available = state.settings.incoming_shared_available === true;
      control.indeterminate = !available;
      control.checked = available && actual;
      status.textContent = available ? (actual ? gxText("开启", "On") : gxText("已暂停", "Paused")) : gxText("不可用", "Unavailable");
      const reason = errorCode === "gx_settings_busy" ? gxText("设置正忙，请重试。", "Settings are busy. Try again.") : errorCode === "gx_local_only" ? gxText("请在实例所在电脑修改。", "Change this on the instance's computer.") : gxText("接收控制不可用。", "Receive control unavailable.");
      showNotice(gxText("接收设置未保存", "Receive setting not saved"), reason, true);
    } catch (_) {
      showNotice(gxText("接收状态未知", "Receive status unknown"), gxText("刷新后核对。", "Refresh to check."), true);
      status.textContent = gxText("状态未知", "Unknown");
    }
  } finally {
    incomingVersion++;
    incomingSaving = false;
    control.disabled = incomingUncertain || state.settings.incoming_shared_available !== true || typeof state.settings.allow_incoming_shared !== "boolean";
    state.signature = "";
    render();
  }
}

function renderIncomingControl(panel) {
  const row = element("div", "gx-incoming");
  const label = element("label", "gx-incoming-label", gxText("接收任务", "Receive jobs"));
  const control = document.createElement("input");
  const available = !incomingUncertain && state.settings.incoming_shared_available === true && typeof state.settings.allow_incoming_shared === "boolean";
  const status = element("span", "gx-incoming-status", incomingSaving ? gxText("保存中", "Saving") : incomingUncertain ? gxText("状态未知", "Unknown") : !available ? gxText("不可用", "Unavailable") : state.settings.allow_incoming_shared ? gxText("开启", "On") : gxText("已暂停", "Paused"));
  control.type = "checkbox";
  control.id = "gx-incoming-shared";
  label.htmlFor = control.id;
  control.checked = available && state.settings.allow_incoming_shared === true;
  control.indeterminate = !available;
  control.disabled = incomingSaving || !available;
  status.setAttribute("aria-live", "polite");
  control.onchange = () => void setIncomingShared(control.checked, control, status);
  row.append(label, status, control);
  panel.appendChild(row);
}

const IMAGE_PATTERN = /\.(png|jpe?g|webp|gif|bmp|tiff?)$/i;
const VIDEO_PATTERN = /\.(mp4|webm|mov|m4v|avi|mkv)$/i;
const AUDIO_PATTERN = /\.(mp3|wav|ogg|m4a|flac|aac)$/i;

function outputReference(path) {
  const normalized = String(path || "").replaceAll(String.fromCharCode(92), "/");
  const marker = "/output/";
  const index = normalized.toLowerCase().lastIndexOf(marker);
  return index < 0 ? null : normalized.slice(index + marker.length) + " [output]";
}

function outputRelativePath(path) {
  const normalized = String(path || "").replaceAll(String.fromCharCode(92), "/").trim();
  if (!normalized) return null;
  if (normalized.endsWith(" [output]")) return normalized.slice(0, -" [output]".length);
  const marker = "/output/";
  const index = normalized.toLowerCase().lastIndexOf(marker);
  return index < 0 ? null : normalized.slice(index + marker.length);
}

function outputViewUrl(path) {
  const relative = outputRelativePath(path);
  if (!relative) return null;
  const segments = relative.split("/");
  return `/view?${new URLSearchParams({ filename: segments.pop(), subfolder: segments.join("/"), type: "output" }).toString()}`;
}

function loadResultToCanvas(path) {
  const result = outputReference(path);
  const loader = mediaKindForPath(path);
  if (!result || !loader) throw new Error(gxText("不支持将此格式加载到画布。", "This format cannot be added to the canvas."));
  const node = globalThis.LiteGraph?.createNode?.(loader.type);
  const graph = app.rootGraph ?? app.graph;
  if (!node || !graph?.add) throw new Error(gxText(`无法创建 ${loader.type} 节点。`, `Cannot create ${loader.type}.`));
  const widget = node.widgets?.find((item) => item.name === "result");
  if (!widget) throw new Error(gxText(`${loader.type} 缺少 result 输入。`, `${loader.type} is missing its result input.`));
  graph.add(node);
  const slot = state.loadCount++;
  node.pos = [80 + (slot % 6) * 360, 80 + Math.floor(slot / 6) * 360];
  widget.value = result;
  widget.callback?.(result);
  ensureSharedPreview(node, loader.kind);
  node._gxSharedRefreshPreview?.();
  app.canvas?.centerOnNode?.(node);
  app.canvas?.selectNode?.(node);
  node.setDirtyCanvas?.(true, true);
  graph.setDirtyCanvas?.(true, true);
  showNotice(gxText("已加载到画布", "Added to canvas"));
}

function renderResults(panel, openDetails = new Set()) {
  const labels = {
    uploading: gxText("传输中", "Transferring"),
    queued: gxText("排队中", "Queued"),
    executing: gxText("运行中", "Running"),
    running: gxText("等待结果", "Waiting for results"),
    downloading: gxText("回收中", "Retrieving"),
    completed: gxText("已完成", "Done"),
    failed: gxText("失败", "Failed"),
  };
  const issues = {
    submission_unknown: [gxText("入队待确认", "Queue receipt unknown"), gxText("先核对目标队列，勿重复提交。", "Check the target queue before retrying.")],
    accepted_save_unconfirmed: [gxText("记录待确认", "Job record unconfirmed"), gxText("目标已接收。请核对目标队列，勿重复提交。", "The target accepted this job. Check its queue before retrying.")],
    monitor_disconnected: [gxText("连接中断", "Connection lost"), gxText("检查目标实例是否在线。", "Check whether the target is online.")],
    remote_status_unknown: [gxText("远端状态未知", "Remote status unknown"), gxText("请核对目标队列和历史。", "Check the target queue and history.")],
    remote_cancelled: [gxText("已取消", "Cancelled"), gxText("目标实例已取消此任务。", "The target cancelled this job.")],
    result_download_interrupted: [gxText("回收中断", "Retrieval interrupted"), gxText("请检查本地共享结果目录。", "Check the local shared results folder.")],
  };
  const filters = element("div", "gx-job-filters");
  filters.setAttribute("role", "group");
  filters.setAttribute("aria-label", gxText("筛选任务", "Filter jobs"));
  for (const [id, label] of [
    ["all", gxText("全部", "All")], ["active", gxText("进行中", "Active")],
    ["done", gxText("已完成", "Done")], ["attention", gxText("待处理", "Needs attention")],
  ]) {
    const button = element("button", "gx-job-filter", label);
    button.id = `gx-shared-filter-${id}`;
    button.type = "button";
    button.setAttribute("aria-pressed", String(state.jobFilter === id));
    button.onclick = () => { state.jobFilter = id; render(); };
    filters.appendChild(button);
  }
  panel.appendChild(filters);
  if (state.jobsUnavailable) {
    const status = element("p", "gx-task-note", gxText("任务状态暂未更新。", "Job status could not be refreshed."));
    status.setAttribute("role", "status");
    panel.appendChild(status);
  }
  let visible = 0;
  for (const job of state.jobs) {
    const issue = Object.hasOwn(issues, job.error_code) ? issues[job.error_code] : null;
    const category = issue || job.status === "failed" ? "attention" : job.status === "completed" ? "done" : "active";
    if (state.jobFilter !== "all" && state.jobFilter !== category) continue;
    visible++;
    const card = document.createElement("article");
    card.className = "gx-result-card";
    card.dataset.jobId = job.id;
    const header = document.createElement("div");
    header.className = "gx-result-header";
    const title = element("b", "", job.workflow_name || gxText("共享任务", "Shared job"));
    const statusLabel = issue?.[0] || (Object.hasOwn(labels, job.status) ? labels[job.status] : gxText("状态未知", "Unknown"));
    const status = element("span", `gx-peer-state gx-job-status-${category}`, statusLabel);
    header.append(title, status);
    card.appendChild(header);
    const time = new Date(Number.isFinite(job.created_at) ? job.created_at * 1000 : NaN);
    const submitted = Number.isFinite(time.getTime())
      ? time.toLocaleString(gxText("zh-CN", "en"), { dateStyle: "short", timeStyle: "short" })
      : "";
    const hasResults = job.status === "completed" && job.outputs?.length && job.outputs_available !== false;
    const resultCount = hasResults ? gxText(`${job.outputs.length} 个结果`, `${job.outputs.length} result(s)`) : "";
    const target = job.target_name || job.target_address || gxText("目标未知", "Unknown target");
    card.appendChild(element("p", "gx-compute-meta", [target, submitted, resultCount].filter(Boolean).join(" · ")));
    if (issue) card.appendChild(element("p", "gx-task-note", issue[1]));
    const details = element("details", "gx-task-details");
    details.id = `gx-task-${job.id}`;
    details.open = openDetails.has(details.id);
    const summary = element("summary", "", hasResults ? gxText("查看结果", "View results") : gxText("任务详情", "Job details"));
    summary.id = `${details.id}-summary`;
    details.appendChild(summary);
    const identifiers = [
      job.target_address,
      gxText(`任务 ID ${job.id}`, `Job ID ${job.id}`),
      job.remote_prompt_id && gxText(`远端 ID ${job.remote_prompt_id}`, `Remote ID ${job.remote_prompt_id}`),
    ];
    details.appendChild(element("div", "gx-task-note", identifiers.filter(Boolean).join(" · ")));
    if (job.status === "completed" && job.outputs?.length) {
      if (job.outputs_available === false) {
        details.appendChild(element("div", "gx-job-result", gxText("结果文件不可用", "Result files unavailable")));
      } else job.outputs.forEach((path, index) => {
        const item = document.createElement("div");
        item.className = "gx-job-result";
        const label = document.createElement("span");
        label.textContent = String(path).split(/[\\\\/]/).pop();
        item.appendChild(label);
        const loader = mediaKindForPath(path);
        if (loader) {
          const button = document.createElement("button");
          button.id = `${details.id}-load-${index}`;
          button.className = "gx-job-load";
          button.textContent = gxText("加载到画布", "Add to canvas");
          button.onclick = () => { try { loadResultToCanvas(path); } catch (error) { showNotice(gxText("加载失败", "Could not load result"), error.message, true); } };
          item.appendChild(button);
        }
        details.appendChild(item);
        const url = outputViewUrl(path);
        if (url && IMAGE_PATTERN.test(path)) {
          const image = document.createElement("img");
          image.className = "gx-result-media";
          image.loading = "lazy";
          image.alt = label.textContent;
          image.src = url;
          details.appendChild(image);
        } else if (url && VIDEO_PATTERN.test(path)) {
          const video = document.createElement("video");
          video.className = "gx-result-media";
          video.controls = true;
          video.preload = "none";
          video.src = url;
          details.appendChild(video);
        } else if (url && AUDIO_PATTERN.test(path)) {
          const audio = document.createElement("audio");
          audio.className = "gx-result-audio";
          audio.controls = true;
          audio.preload = "none";
          audio.src = url;
          details.appendChild(audio);
        }
      });
    } else if (job.status === "failed" && !issue) {
      const error = document.createElement("div");
      error.className = "gx-job-result";
      const exception = Array.isArray(job.error) ? job.error.find((entry) => Array.isArray(entry) && entry[0] === "execution_error")?.[1] : null;
      error.textContent = (typeof job.error === "string" ? job.error : exception?.exception_message)
        || gxText("请查看目标实例的执行日志。", "Check the target's execution log.");
      details.appendChild(error);
    } else if (job.status === "completed") {
      details.appendChild(element("div", "gx-job-result", gxText("没有可回收的媒体文件", "No media files to retrieve")));
    }
    card.appendChild(details);
    panel.appendChild(card);
  }
  if (!visible) {
    const message = state.jobFilter === "all" ? gxText("暂无共享任务", "No shared jobs yet") : gxText("此筛选下暂无任务", "No jobs match this filter");
    panel.appendChild(element("div", "gx-results-empty", message));
  }
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function peerCountLabel() {
  if (state.discoveryError) return gxText("发现失败", "Discovery failed");
  return gxText(`${state.peers.length} 台在线`, `${state.peers.length} online`);
}

function render(root = document.querySelector("#gx-shared-compute")) {
  if (!root) return false;
  const active = document.activeElement;
  const focusId = active && root.contains(active) && active.id ? active.id : null;
  const openDetails = new Set(Array.from(root.querySelectorAll("details[open][id]"), (item) => item.id));
  const restoreFocus = () => {
    if (!focusId) return;
    const replacement = document.getElementById(focusId);
    if (replacement && root.contains(replacement) && !replacement.disabled) replacement.focus();
  };
  root.replaceChildren();
  const panel = element("section", "gx-compute-panel");
  const heading = element("div", "gx-compute-heading");
  heading.append(element("h3", "gx-compute-title", gxText("共享算力", "Shared Compute")), element("span", "gx-compute-count", peerCountLabel()));
  panel.appendChild(heading);
  const tabs = element("div", "gx-shared-tabs");
  [["compute", gxText("实例", "Instances")], ["results", gxText("任务", "Jobs")]].forEach(([id, label]) => {
    const tab = element("button", "gx-shared-tab" + (state.activeTab === id ? " is-active" : ""), label);
    tab.id = `gx-shared-tab-${id}`;
    tab.setAttribute("aria-pressed", String(state.activeTab === id));
    tab.onclick = () => { state.activeTab = id; render(); };
    tabs.appendChild(tab);
  });
  panel.appendChild(tabs);
  if (state.activeTab === "results") { renderResults(panel, openDetails); root.appendChild(panel); restoreFocus(); return; }
  renderIncomingControl(panel);
  const rules = element("details", "gx-incoming-details");
  rules.id = "gx-shared-receive-rules";
  rules.open = openDetails.has(rules.id);
  const summary = element("summary", "", gxText("接收规则", "Receive rules"));
  summary.id = "gx-shared-rules";
  rules.append(summary, element("p", "", gxText(
    "暂停接收后，其他电脑无法直接提交新任务。本机运行、已入队及已通过接收检查的任务可继续。本机代理不受此开关限制。",
    "Pausing blocks new jobs sent directly from other computers. Local, queued, and already accepted jobs can continue. Local proxies are outside this control.",
  )));
  panel.appendChild(rules);
  const runControls = element("div", "gx-run-controls");
  const run = element("button", "");
  run.id = "gx-shared-run";
  run.type = "button";
  run.onclick = runShared;
  run.setAttribute("aria-describedby", "gx-shared-target");
  const targetLabel = element("span", "gx-run-target");
  targetLabel.id = "gx-shared-target";
  runControls.append(run, targetLabel);
  const list = element("div", "gx-compute-list");
  const peers = peersByPriority();
  const lockUnavailable = state.lockUnavailable === true;
  const target = selectedPeer();
  if (lockUnavailable) {
    const warning = element("div", "gx-compute-empty", gxText("排除列表不可用", "Exclusion list unavailable"));
    const reset = document.createElement("button");
    reset.className = "gx-compute-icon";
    reset.textContent = gxText("重置排除列表", "Reset exclusions");
    reset.onclick = (event) => { event.stopPropagation(); void resetLockState(); };
    warning.append(reset);
    list.appendChild(warning);
  } else if (!peers.length) {
    const emptyText = state.discoveryError
      ? gxText("无法发现实例，请检查连接。", "Discovery failed. Check the connection.")
      : gxText("未发现其他实例", "No other instances found");
    list.appendChild(element("div", "gx-compute-empty", emptyText));
  }
  peers.forEach((peer, index) => {
    const hardware = peer.hardware || {};
    const locked = isLocked(peer);
    const paused = peer.allow_incoming_shared === false;
    const incomingLabel = locked ? gxText("已排除", "Excluded") : paused ? gxText("已暂停", "Paused") : peer.allow_incoming_shared === true ? gxText("可用", "Ready") : gxText("状态未知", "Unknown");
    const row = element("div", `gx-compute-row${peer.id === target?.id ? " is-selected" : ""}${locked || paused || lockUnavailable ? " is-locked" : ""}`);
    row.draggable = true;
    row.dataset.peerId = peer.id;

    const machine = element("div", "gx-compute-machine");
    const nameRow = element("div", "gx-compute-name");
    const select = element("button", "gx-peer-select", peer.name || peer.host);
    select.id = `gx-peer-select-${peer.id}`;
    select.type = "button";
    select.disabled = !canSelectPeer(peer);
    select.title = peer.name || peer.host;
    select.setAttribute("aria-pressed", String(peer.id === target?.id));
    select.onclick = () => { if (canSelectPeer(peer)) setSelection(peer.id); };
    const ready = !locked && !lockUnavailable && peer.allow_incoming_shared === true;
    nameRow.append(select, element("span", ready ? "gx-peer-state gx-peer-ready" : "gx-peer-state", incomingLabel));
    machine.append(nameRow, element("div", "gx-compute-meta", `${peer.host}:${peer.port}${hardware.device ? ` · ${hardware.device}` : ""}`));

    const metrics = element("div", "gx-compute-metrics");
    if (hardware.vram_total > 0 && Number.isFinite(hardware.vram_free)) {
      const free = formatGiB(hardware.vram_free);
      metrics.appendChild(element("span", "", gxText(`${free} 显存空闲`, `${free} VRAM free`)));
    }
    const running = peer.running || 0;
    const waiting = Math.max(0, (peer.queue || 0) - running);
    if (running) metrics.appendChild(element("span", "", gxText(`${running} 个运行`, `${running} running`)));
    metrics.appendChild(element("span", "", waiting ? gxText(`${waiting} 个等待`, `${waiting} waiting`) : gxText("无排队", "No queue")));

    const options = element("details", "gx-peer-options");
    options.id = `gx-peer-options-${peer.id}`;
    options.open = openDetails.has(options.id);
    const optionLabel = element("summary", "", gxText("实例选项", "Instance options"));
    optionLabel.id = `${options.id}-summary`;
    options.appendChild(optionLabel);
    const actions = element("div", "gx-compute-actions");
    const lockControl = element("label", "gx-lock-control");
    lockControl.title = gxText("仅在此浏览器排除该实例。", "Exclude this instance in this browser only.");
    const checkbox = document.createElement("input");
    checkbox.id = `${options.id}-exclude`;
    checkbox.type = "checkbox";
    checkbox.checked = locked && !lockUnavailable;
    checkbox.disabled = lockUnavailable;
    lockControl.append(checkbox, element("span", "", gxText("排除", "Exclude")));
    const order = element("div", "gx-compute-order");
    const up = element("button", "gx-compute-icon", "↑");
    up.id = `${options.id}-up`;
    up.title = gxText("提高优先级", "Raise priority");
    up.setAttribute("aria-label", up.title);
    up.disabled = index === 0;
    const down = element("button", "gx-compute-icon", "↓");
    down.id = `${options.id}-down`;
    down.title = gxText("降低优先级", "Lower priority");
    down.setAttribute("aria-label", down.title);
    down.disabled = index === peers.length - 1;
    order.append(up, down);
    actions.append(lockControl, order);
    options.appendChild(actions);
    row.append(machine, metrics, options);
    row.onclick = (event) => {
      if (!event.target.closest("button, details") && canSelectPeer(peer)) setSelection(peer.id);
    };

    row.addEventListener("dragstart", (event) => {
      event.dataTransfer?.setData("text/plain", peer.id);
      event.dataTransfer.effectAllowed = "move";
      row.classList.add("is-dragging");
    });
    row.addEventListener("dragover", (event) => {
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
      row.classList.add("is-over");
    });
    row.addEventListener("dragleave", () => row.classList.remove("is-over"));
    row.addEventListener("drop", (event) => {
      event.preventDefault();
      const fromId = event.dataTransfer?.getData("text/plain");
      row.classList.remove("is-over");
      reorder(fromId, peer.id);
    });
    row.addEventListener("dragend", () => row.classList.remove("is-dragging"));
    checkbox.onchange = (event) => { event.stopPropagation(); void setLock(peer.id, event.target.checked === true); };
    checkbox.onclick = (event) => event.stopPropagation();
    up.onclick = (event) => { event.stopPropagation(); move(peer.id, -1); };
    down.onclick = (event) => { event.stopPropagation(); move(peer.id, 1); };
    list.appendChild(row);
  });
  panel.append(list, runControls); root.appendChild(panel); updateRunButton(); restoreFocus();
}

function updateRunButton() {
  const button = document.querySelector("#gx-shared-run");
  if (!button) return;
  const peer = selectedPeer();
  button.disabled = !sharedEnabled || pushInFlight || !peer;
  button.textContent = pushInFlight ? gxText("提交中…", "Submitting…") : gxText("共享运行", "Run on instance");
  button.title = state.discoveryError ? gxText("发现失败", "Discovery failed")
    : state.lockUnavailable ? gxText("排除列表不可用", "Exclusion list unavailable")
    : peer ? peer.host + ":" + peer.port
    : state.selectedId ? gxText("所选实例不可用", "Selected instance unavailable")
    : gxText("请先选择实例", "Select an instance first");
  const label = document.querySelector("#gx-shared-target");
  if (label) label.textContent = peer ? gxText("目标 · " + (peer.name || peer.host), "Target · " + (peer.name || peer.host)) : button.title;
}

async function runShared() {
  if (pushInFlight) return;
  if (!sharedEnabled) {
    showNotice(gxText("共享算力已关闭", "Shared Compute is off"));
    return;
  }
  const peer = selectedPeer();
  if (!peer) {
    showNotice(gxText("请选择可用实例", "Choose an available instance"), gxText("在共享算力侧栏选择目标。", "Select a target in the Shared Compute sidebar."));
    return;
  }
  const stillSelected = () => {
    const current = selectedPeer();
    return sharedEnabled && current?.id === peer.id && current.host === peer.host && current.port === peer.port;
  };
  const showJobs = (filter) => {
    state.activeTab = "results";
    state.jobFilter = filter;
    render();
    document.getElementById?.("gx-shared-tab-results")?.focus();
    void refresh();
  };
  pushInFlight = true;
  updateRunButton();
  let receipt;
  try {
    receipt = await push(peer, stillSelected);
  } catch (error) {
    const uncertain = error.cause === "submission_unknown";
    const unrecorded = error.cause === "accepted_save_unconfirmed";
    const title = uncertain
      ? gxText("入队状态未知", "Queue status unknown")
      : unrecorded ? gxText("已接收 · 记录待确认", "Accepted · record unconfirmed")
      : gxText("共享运行失败", "Shared run failed");
    const detail = uncertain || unrecorded
      ? `${error.message}\n${gxText("目标", "Target")} · ${peer.host}:${peer.port}`
      : error.message;
    showNotice(title, detail, true);
    if (uncertain || unrecorded) showJobs("attention");
  } finally {
    pushInFlight = false;
    updateRunButton();
  }
  if (!receipt) return;
  if (typeof receipt.job?.id === "string") {
    jobReceiptVersion++;
    state.jobs = [receipt.job, ...state.jobs.filter((job) => job.id !== receipt.job.id)].slice(0, 20);
  }
  showNotice(gxText("已提交", "Job submitted"), gxText("可在任务页跟踪。", "Track it under Jobs."));
  showJobs("all");
}

function sharedNodeKind(node) {
  if (node?.type === "GXSharedLoadImage" || node?.comfyClass === "GXSharedLoadImage") return "image";
  if (node?.type === "GXSharedLoadVideo" || node?.comfyClass === "GXSharedLoadVideo") return "video";
  if (node?.type === "GXSharedLoadAudio" || node?.comfyClass === "GXSharedLoadAudio") return "audio";
  return null;
}

function registerTab() {
  const manager = app.extensionManager;
  if (tabRegistered || typeof manager?.registerSidebarTab !== "function") return;
  manager.registerSidebarTab({
    id: TAB_ID,
    icon: "icon-[lucide--activity]",
    title: gxText("共享算力", "Shared Compute"),
    tooltip: gxText("局域网实例与结果", "LAN instances and results"),
    type: "custom",
    render: (el) => {
      el.id = TAB_ID;
      render(el);
    },
  });
  tabRegistered = true;
}

function unregisterTab() {
  if (!tabRegistered) return;
  app.extensionManager?.unregisterSidebarTab?.(TAB_ID);
  tabRegistered = false;
}

function startPolling() {
  if (pollTimer || document.visibilityState !== "visible") return;
  pollTimer = setInterval(refresh, 3000);
  void refresh();
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
  ++refreshVersion;
  activeRefresh?.abort();
  activeRefresh = null;
  state.peers = [];
  state.discoveryError = true;
  state.signature = "";
  // Keep the explicit selection and any already started push intact.
  try { render(); } catch (_) { /* Still invalidate the actionable target. */ }
  finally { updateRunButton(); }
}

function applyFeature(enabled) {
  const settled = enabled === sharedEnabled && (enabled ? tabRegistered : !tabRegistered);
  if (settled) return;
  sharedEnabled = enabled;
  if (enabled) {
    registerTab();
    startPolling();
    return;
  }
  stopPolling();
  unregisterTab();
}

app.registerExtension({
  name: "gxnodes.shared.compute",
  commands: [{ id: "GXNodes.SharedCompute.Run", label: () => gxText("共享运行", "Run on shared instance"), function: runShared }],
  menuCommands: [{ path: ["Extensions", "GXNodes"], commands: ["GXNodes.SharedCompute.Run"] }],
  loadedGraphNode(node) {
    const kind = sharedNodeKind(node);
    if (!kind) return;
    requestAnimationFrame(() => {
      ensureSharedPreview(node, kind);
      node._gxSharedRefreshPreview?.();
    });
  },
  async setup() {
    ensureStyles();
    installLockSync();
    applyFeature((await gxConfig())?.enable_shared_compute !== false);

    document.addEventListener("visibilitychange", () => {
      if (!sharedEnabled) return;
      if (document.visibilityState === "visible") startPolling();
      else stopPolling();
    });

    onGxSettingsChanged((patch) => {
      if (!("enable_shared_compute" in patch)) return;
      void gxConfig(true).then((features) => applyFeature(features?.enable_shared_compute !== false));
    });

  },
});
