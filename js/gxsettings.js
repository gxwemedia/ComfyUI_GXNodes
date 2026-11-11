import { app } from "../../../scripts/app.js";
import { authorizeGxSession, gxJson, invalidateConfig, notifyGxSettingsChanged } from "./gxapi.js";
import { gxText } from "./gxi18n.js";
import { openSharedTokenSettings } from "./gxtoken.js";

const LIVE_HINT = "保存后立即生效，无需重启 ComfyUI。";
const NODE_KEYS = new Set(["enable_comfypilot", "enable_shared_compute"]);

const ENTRIES = [
  {
    id: "GXNodes.Topaz.FfmpegPath",
    key: "topaz_ffmpeg_path",
    type: "text",
    defaultValue: "",
    category: ["GXNodes", "Topaz Video AI", "Topaz FFmpeg path"],
    name: "Topaz FFmpeg path",
    tooltip: "Enter the local absolute path. Leave blank to detect it automatically.",
  },
  {
    id: "GXNodes.ComfyPilot.Enabled",
    key: "enable_comfypilot",
    type: "boolean",
    defaultValue: true,
    category: ["GXNodes", "企业版功能 · ComfyPilot", "启用 ComfyPilot 节点"],
    name: "启用 ComfyPilot 节点",
    tooltip: "关闭后 GX ComfyPilot / Agnes 节点会从节点菜单移除。" + LIVE_HINT,
  },
  {
    id: "GXNodes.ComfyPilot.Url",
    key: "comfypilot_url",
    type: "text",
    defaultValue: "http://127.0.0.1:39112",
    category: ["GXNodes", "企业版功能 · ComfyPilot", "ComfyPilot 服务地址"],
    name: "ComfyPilot 服务地址",
    tooltip: "新建的 ComfyPilot 连接节点会使用该默认地址。" + LIVE_HINT,
  },
  {
    id: "GXNodes.ComfyPilot.AllowRemote",
    key: "allow_remote_comfypilot",
    type: "boolean",
    defaultValue: false,
    category: ["GXNodes", "企业版功能 · ComfyPilot", "允许远程 ComfyPilot 地址"],
    name: "允许远程 ComfyPilot 地址",
    tooltip: "默认只允许本机地址，开启后可连接局域网内的 ComfyPilot 服务。" + LIVE_HINT,
  },
  {
    id: "GXNodes.SharedCompute.Enabled",
    key: "enable_shared_compute",
    type: "boolean",
    defaultValue: true,
    category: ["GXNodes", "共享算力", "启用共享算力"],
    name: "启用共享算力",
    tooltip: "关闭后停止局域网发现并移除共享加载节点。" + LIVE_HINT,
  },
  {
    id: "GXNodes.SharedCompute.ResultRetentionDays",
    key: "result_retention_days",
    type: "number",
    defaultValue: 0,
    attrs: { min: 0, max: 3650, step: 1 },
    category: ["GXNodes", "共享算力", "结果保留天数"],
    name: "结果保留天数",
    tooltip: "0 表示永久保留；大于 0 时自动清理过期的共享结果与任务记录。" + LIVE_HINT,
  },
  {
    id: "GXNodes.SharedCompute.TransferRetentionHours",
    key: "transfer_retention_hours",
    type: "number",
    defaultValue: 24,
    attrs: { min: 0, max: 8760, step: 1 },
    category: ["GXNodes", "共享算力", "传输资源保留小时"],
    name: "传输资源保留小时",
    tooltip: "0 表示永久保留 input/temp 下的共享传输资源。" + LIVE_HINT,
  },
];

let hydrating = false;
const inflight = new Map();
// Server-side truth per key, so a re-emitted onChange with the same value is not persisted again.
const known = new Map();

function sameValue(left, right) {
  if (typeof left === "number" || typeof right === "number") return Number(left) === Number(right);
  if (typeof left === "boolean" || typeof right === "boolean") return Boolean(left) === Boolean(right);
  return String(left ?? "") === String(right ?? "");
}

function waitForSettingStore(timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve) => {
    const poll = () => {
      if (typeof unwrap(app.extensionManager?.setting)?.set === "function") return resolve(true);
      if (Date.now() >= deadline) return resolve(false);
      window.setTimeout(poll, 200);
    };
    poll();
  });
}

// `extensionManager` members may be Vue refs or already-unwrapped values.
function unwrap(value) {
  return value && typeof value === "object" && "value" in value ? value.value : value;
}

function applySetting(id, value) {
  const store = unwrap(app.extensionManager?.setting);
  if (typeof store?.set === "function") return store.set(id, value);
  if (typeof store?.setSettingValue === "function") return store.setSettingValue(id, value);
  return undefined;
}

async function refreshNodeDefinitions() {
  for (const candidate of [app.extensionManager?.command, app.extensionManager?.commands]) {
    const store = unwrap(candidate);
    if (typeof store?.execute === "function") {
      try {
        await store.execute("Comfy.RefreshNodeDefinitions");
        return true;
      } catch (error) {
        console.warn("[GXNodes] node definition refresh failed", error);
      }
    }
  }
  return false;
}

function notify(title, detail, severity = "error") {
  const toast = app.extensionManager?.toast;
  if (toast?.[severity]) toast[severity](title, { detail, life: 4200 });
  else toast?.add?.({ severity, summary: title, detail, life: 4200 });
}

async function send(entry, value) {
  const body = await gxJson("/gx/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ [entry.key]: value }),
  });
  const saved = body.settings?.[entry.key];
  known.set(entry.key, saved ?? value);
  const active = inflight.get(entry.key);
  if (active) active.current = saved ?? value;
  if (saved !== undefined && inflight.get(entry.key)?.pending === null) {
    applySetting(entry.id, saved);
  }
  invalidateConfig();
  notifyGxSettingsChanged({ [entry.key]: saved ?? value });
  if (NODE_KEYS.has(entry.key)) await refreshNodeDefinitions();
}

async function persist(entry, value) {
  // Serialize per key: a burst of changes sends the in-flight request plus the
  // latest value at most, instead of one request per event.
  const active = inflight.get(entry.key);
  if (active) {
    active.pending = value;
    return;
  }
  const slot = { pending: null, current: value };
  inflight.set(entry.key, slot);
  try {
    let next = value;
    let failed = false;
    while (next !== null) {
      slot.current = next;
      try {
        await send(entry, next);
        failed = false;
      } catch (error) {
        failed = true;
        notify(gxText("设置保存失败", "Could not save settings"), error.message);
        if (slot.pending === null) {
          try {
            const body = await gxJson("/gx/settings");
            const saved = body.settings?.[entry.key];
            if (saved !== undefined && slot.pending === null) {
              known.set(entry.key, saved);
              slot.current = saved; // suppress the programmatic onChange echo
              applySetting(entry.id, saved);
              invalidateConfig();
              notifyGxSettingsChanged({ [entry.key]: saved });
            }
          } catch {
            notify(gxText("设置未保存", "Settings not saved"), gxText("无法确认服务端当前值，请重新打开设置后再试。", "Reopen Settings to check the current value."));
          }
        }
      }
      next = slot.pending;
      slot.pending = null;
    }
    if (failed) return;
    notify(gxText("设置已保存", "Settings saved"), undefined, "success");
  } catch (error) {
    notify(gxText("设置保存失败", "Could not save settings"), error.message);
  } finally {
    inflight.delete(entry.key);
  }
}

function onChange(entry, value, oldValue) {
  // ComfyUI invokes onChange(defaultValue, undefined) when the setting is registered,
  // and onChange(newValue, previousValue) for real changes.
  if (oldValue === undefined) return;
  if (hydrating) return;
  const active = inflight.get(entry.key);
  if (known.has(entry.key) && sameValue(known.get(entry.key), value)
      && (!active || sameValue(active.current, value))) return;
  void persist(entry, value);
}

async function hydrate() {
  if (!await waitForSettingStore()) {
    console.warn("[GXNodes] setting store unavailable; panel keeps default values");
    return;
  }
  try {
    const body = await gxJson("/gx/settings");
    const settings = body.settings || {};
    hydrating = true;
    for (const entry of ENTRIES) {
      const value = settings[entry.key];
      if (value === undefined) continue;
      known.set(entry.key, value);
      applySetting(entry.id, value);
    }
    if (typeof settings.enable_shared_compute === "boolean") {
      notifyGxSettingsChanged({ enable_shared_compute: settings.enable_shared_compute });
    }
  } catch (error) {
    console.warn("[GXNodes] settings hydrate failed", error);
  } finally {
    window.setTimeout(() => { hydrating = false; }, 600);
  }
}

function actionButton(label, action) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    try {
      await action();
    } catch {
      notify(gxText("操作未完成", "Action incomplete"), gxText("请检查连接和授权状态。", "Check your connection and authorization."));
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

app.registerExtension({
  name: "gxnodes.settings",
  settings: [
    {
      id: "GXNodes.Topaz.SupportedVersion",
      category: ["GXNodes", "Topaz Video AI", "Supported version"],
      name: "Supported Topaz Video AI version",
      tooltip: "Reference version used for the command mappings.",
      defaultValue: null,
      type: () => {
        const version = document.createElement("span");
        version.textContent = "6.2.1";
        return version;
      },
    },
    ...ENTRIES.map((entry) => ({
      id: entry.id,
      category: entry.category,
      name: entry.name,
      tooltip: entry.tooltip,
      type: entry.type,
      defaultValue: entry.defaultValue,
      ...(entry.attrs ? { attrs: entry.attrs } : {}),
      onChange: (value, oldValue) => onChange(entry, value, oldValue),
    })),
    {
      id: "GXNodes.SharedCompute.AuthorizeAction",
      category: ["GXNodes", "共享算力", "连接授权"],
      name: "连接授权",
      tooltip: "为当前页面授权访问此 ComfyUI 的共享功能。",
      defaultValue: null,
      type: () => actionButton(gxText("连接授权", "Authorize connection"), async () => {
        if (await authorizeGxSession()) await hydrate();
      }),
    },
    {
      id: "GXNodes.SharedCompute.ManageTokenAction",
      category: ["GXNodes", "共享算力", "管理共享令牌"],
      name: "管理共享令牌",
      tooltip: "设置或清除此实例的共享令牌。",
      defaultValue: null,
      type: () => actionButton(gxText("管理共享令牌", "Manage shared token"), async () => {
        if (await authorizeGxSession()) {
          await hydrate();
          await openSharedTokenSettings();
        }
      }),
    },
    {
      id: "GXNodes.Enterprise.Mock",
      category: ["GXNodes", "企业预留", "企业接入（模拟，未实现）"],
      name: "企业接入（模拟，未实现）",
      tooltip: "仅供界面演示，不提供企业认证、权限或审计，也不影响局域网共享。",
      type: "boolean",
      defaultValue: false,
    },
  ],
  async setup() {
    queueMicrotask(hydrate);
  },
});
