import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";
import { gxText } from "./gxi18n.js";

function parse(value) {
  return String(value || "").split("\n").filter(x => x.trim() && x.trim() !== "!directory-change").map(line => {
    const [name, ...resources] = line.split("|").map(x => x.trim());
    return { name, resources: resources.join(" | ") };
  });
}
function encode(rows) {
  return rows.map(row => [row.name, ...row.resources.split("|").map(x => x.trim()).filter(Boolean)].join(" | ")).join("\n");
}
async function getCatalog(folder) {
  const session = await api.fetchApi("/gx/session", { cache: "no-store" });
  const sessionBody = session.ok ? await session.json() : {};
  const headers = { "Content-Type": "application/json" };
  if (typeof sessionBody.token === "string") headers["X-GX-Token"] = sessionBody.token;
  const response = await api.fetchApi("/gx/llm/skills", { method: "POST", headers, body: JSON.stringify({ folder }) });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.error || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return { skills: Array.isArray(body.skills) ? body.skills : [], location: String(body.location || ""), source: body.source };
}
function installStyles() {
  if (document.getElementById("gx-skill-picker-style")) return;
  const style = document.createElement("style"); style.id = "gx-skill-picker-style";
  style.textContent = `
.gx-skills{font:12px var(--font-family,Inter,system-ui,sans-serif);color:var(--input-text,#ddd);box-sizing:border-box;padding:3px 6px 5px;overflow:auto;background:transparent}
.gx-skills *{box-sizing:border-box}.gx-skills-bar{display:flex;align-items:center;gap:6px;min-height:28px}
.gx-skills-title{font-weight:600;flex:1;color:var(--fg-color,#ddd)}.gx-skills-hint{color:var(--descrip-text,#999);font-size:11px;line-height:1.35;margin:1px 0 5px}
.gx-skills button{font:inherit;cursor:pointer;color:var(--input-text,#ddd);background:var(--comfy-input-bg,#353535);border:1px solid var(--border-color,#515151);border-radius:5px;min-height:25px;padding:2px 8px}
.gx-skills button:hover{filter:brightness(1.18)}.gx-skills button:focus-visible,.gx-skills select:focus-visible,.gx-skills input:focus-visible{outline:2px solid var(--primary-color,#6e9bc9);outline-offset:1px}
.gx-skills button:disabled{opacity:.38;cursor:default}.gx-skills .gx-icon{width:26px;padding:0;font-size:15px;line-height:22px}.gx-skills-list{display:grid;gap:5px}
.gx-skills-row{border:1px solid var(--border-color,#4b4b4b);border-radius:6px;padding:5px;background:var(--comfy-input-bg,#303030)}
.gx-skills-line{display:flex;gap:4px;align-items:center}.gx-skills select,.gx-skills input{min-width:0;color:var(--input-text,#ddd);background:var(--comfy-menu-bg,#252525);border:1px solid var(--border-color,#505050);border-radius:4px;padding:4px 6px;font:inherit}
.gx-skills select{flex:1;height:26px}.gx-skills input{width:100%;margin-top:4px}.gx-skills details{margin-top:4px;color:var(--descrip-text,#aaa)}.gx-skills summary{cursor:pointer;font-size:11px}
.gx-skills-status{color:var(--descrip-text,#aaa);font-size:11px;line-height:1.4;padding:3px 0}.gx-skills-error{color:var(--error-text,#e6a0a0)}.gx-skills-error button{margin-left:6px}.gx-skills-error details{margin:3px 0 0;overflow-wrap:anywhere}
`;
  document.head.append(style);
}
function install(node) {
  const raw = node.widgets?.find(w => w.name === "skills");
  const folder = node.widgets?.find(w => w.name === "skill_folder");
  if (!raw || !folder || node._gxSkillPicker) return;
  installStyles(); raw.type = "hidden"; raw.computeSize = () => [0, -4];
  if (raw.element) raw.element.style.display = "none";
  if (!folder.label || folder.label === folder.name) folder.label = gxText("技能目录", "Skills folder");
  const host = document.createElement("div"); host.className = "gx-skills";
  let catalog = [], rows = parse(raw.value), sequence = 0, resourceOpen = new Set();
  let pending = String(raw.value || "").split("\n").includes("!directory-change"), lastFolder = String(folder.value || "");
  const bar = document.createElement("div"); bar.className = "gx-skills-bar";
  const title = document.createElement("span"); title.className = "gx-skills-title"; title.textContent = gxText("技能", "Skills");
  const add = document.createElement("button"); add.textContent = gxText("+ 添加", "+ Add"); add.title = gxText("添加技能", "Add a skill");
  const refresh = document.createElement("button"); refresh.className = "gx-icon"; refresh.textContent = "↻";
  refresh.title = gxText("刷新技能目录", "Refresh skills"); refresh.setAttribute("aria-label", refresh.title);
  const hint = document.createElement("div"); hint.className = "gx-skills-hint"; hint.textContent = gxText("目录留空时使用默认技能。", "Leave the folder blank to use default skills.");
  const status = document.createElement("div"); status.className = "gx-skills-status";
  const location = document.createElement("details"); location.className = "gx-skills-hint";
  const locationLabel = document.createElement("summary"); locationLabel.textContent = gxText("目录来源", "Folder source");
  const locationText = document.createElement("div"); locationText.style.overflowWrap = "anywhere"; location.append(locationLabel,locationText);
  const list = document.createElement("div"); list.className = "gx-skills-list";
  bar.append(title,add,refresh); host.append(bar,hint,status,location,list);
  const commit = () => { raw.value = (pending ? "!directory-change\n" : "") + encode(rows); node.setDirtyCanvas?.(true, true); };
  function adjust() {
    node.setDirtyCanvas?.(true, true);
    const desired = node.computeSize?.();
    if (desired && node.size && node.setSize && Math.abs(desired[1] - node.size[1]) > 1) node.setSize([node.size[0],desired[1]]);
  }
  const button = (label, title, action, disabled = false) => {
    const el = document.createElement("button"); el.className = "gx-icon"; el.textContent = label; el.title = title; el.setAttribute("aria-label",title); el.disabled = disabled; el.onclick = action; return el;
  };
  function render() {
    list.replaceChildren();
    rows.forEach((row, index) => {
      const box = document.createElement("div"); box.className = "gx-skills-row";
      const line = document.createElement("div"); line.className = "gx-skills-line";
      const select = document.createElement("select"); select.setAttribute("aria-label", gxText(`第 ${index + 1} 个技能`, `Skill ${index + 1}`));
      const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = gxText("选择技能…", "Select a skill…"); select.append(placeholder);
      if (row.name && !catalog.some(x => x.name === row.name)) {
        const missing = document.createElement("option"); missing.value = row.name; missing.textContent = gxText(`${row.name}（不可用）`, `${row.name} (unavailable)`); select.append(missing);
      }
      for (const item of catalog) { const opt = document.createElement("option"); opt.value = item.name; opt.textContent = item.name; opt.title = item.description; select.append(opt); }
      select.value = row.name;
      select.onchange = () => {
        if (rows.some((other, i) => i !== index && other.name.toLowerCase() === select.value.toLowerCase() && select.value)) {
          status.textContent = gxText("该技能已添加。", "That skill is already selected."); select.value = row.name; return;
        }
        row.name = select.value; commit();
      };
      const detail = document.createElement("details"); detail.open = resourceOpen.has(row) || !!row.resources;
      const summary = document.createElement("summary");
      const resourceLabel = () => row.resources ? gxText("资源 · 已设置", "Resources added") : gxText("可选资源", "Optional resources");
      summary.textContent = resourceLabel();
      const resources = document.createElement("input"); resources.type = "text"; resources.placeholder = gxText("相对路径，以 | 分隔", "Relative paths separated by |"); resources.value = row.resources;
      resources.setAttribute("aria-label", gxText(`第 ${index + 1} 个技能的资源路径`, `Resource paths for skill ${index + 1}`));
      resources.oninput = () => { row.resources = resources.value.replace(/[\r\n]/g, ""); summary.textContent = resourceLabel(); commit(); };
      detail.ontoggle = () => { if (detail.open) resourceOpen.add(row); else resourceOpen.delete(row); adjust(); };
      detail.append(summary,resources);
      line.append(select,
        button("↑", gxText("上移", "Move up"), () => { [rows[index-1], rows[index]] = [rows[index], rows[index-1]]; commit(); render(); },index===0),
        button("↓", gxText("下移", "Move down"), () => { [rows[index+1], rows[index]] = [rows[index], rows[index+1]]; commit(); render(); },index===rows.length-1),
        button("×", gxText("移除", "Remove"), () => { resourceOpen.delete(row); rows.splice(index,1); commit(); render(); }));
      box.append(line,detail); list.append(box);
    });
    adjust();
  }
  async function load() {
    const current = ++sequence; status.className = "gx-skills-status"; status.textContent = gxText("正在读取技能目录…", "Loading skills…");
    try {
      const result = await getCatalog(String(folder.value || ""));
      if (current !== sequence) return;
      catalog = result.skills;
      const source = result.source === "custom" ? gxText("指定目录", "Custom folder") : gxText("环境默认目录", "Default folder");
      locationText.textContent = `${source} · ${result.location}`;
      status.replaceChildren();
      if (pending) {
        const text = document.createElement("span"); text.textContent = gxText("目录已更改，请确认已有技能。", "Folder changed. Confirm the selected skills.");
        const confirm = document.createElement("button"); confirm.textContent = gxText("确认选择", "Confirm selection");
        confirm.onclick = () => {
          const missing = rows.find(row => row.name && !catalog.some(item => item.name === row.name));
          if (missing) { text.textContent = gxText(`新目录缺少 ${missing.name}，请移除或重新选择。`, `${missing.name} is missing. Remove it or select another skill.`); return; }
          pending = false; commit(); load();
        };
        status.append(text,confirm);
      } else status.textContent = catalog.length ? gxText(`${catalog.length} 个可用技能`, `Skills available: ${catalog.length}`) : gxText("目录中没有有效技能。", "No skills found in this folder.");
    } catch (error) {
      if (current !== sequence) return;
      status.className = "gx-skills-status gx-skills-error"; status.replaceChildren();
      const message = document.createElement("span"); message.textContent = error.status === 405 || error.status === 404
        ? gxText("技能服务尚未加载，请保存工作流后重启 ComfyUI。", "Skills are unavailable. Save your workflow and restart ComfyUI.")
        : gxText("技能目录加载失败。", "Could not load skills.");
      const retry = document.createElement("button"); retry.textContent = gxText("重试", "Retry"); retry.onclick = load;
      const details = document.createElement("details"); const summary = document.createElement("summary"); summary.textContent = gxText("详情", "Details");
      const diagnostic = document.createElement("div"); diagnostic.textContent = error.message || gxText("请求失败", "Request failed"); details.append(summary,diagnostic); status.append(message,retry,details);
    }
    render();
  }
  add.onclick = () => { if (rows.some(row => !row.name)) { status.textContent = gxText("请先填写空白项。", "Select a skill in the empty row first."); return; } rows.push({ name: "", resources: "" }); render(); };
  refresh.onclick = load;
  const callback = folder.callback; folder.callback = function(...args) {
    callback?.apply(this,args);
    const current = String(folder.value || "");
    if (current !== lastFolder && rows.some(row => row.name)) { pending = true; commit(); }
    lastFolder = current; load();
  };
  const widget = node.addDOMWidget("skill_picker", "gx_skill_picker", host, { serialize: false, getValue: () => "", setValue() {} });
  widget.computeSize = width => [Math.max(260,width || 260), Math.min(420, 102 + rows.reduce((height,row) => height + 40 + ((resourceOpen.has(row) || row.resources) ? 34 : 0),0))];
  const onConfigure = node.onConfigure;
  node.onConfigure = function(...args) { const result = onConfigure?.apply(this,args); rows = parse(raw.value); pending = String(raw.value || "").split("\n").includes("!directory-change"); lastFolder = String(folder.value || ""); resourceOpen = new Set(); render(); load(); return result; };
  node._gxSkillPicker = { render, load }; render(); load();
}
app.registerExtension({
  name: "gxnodes.skill-picker",
  nodeCreated(node) {
    if ((node.comfyClass || node.type) === "GXSkillConfig") install(node);
  },
});
