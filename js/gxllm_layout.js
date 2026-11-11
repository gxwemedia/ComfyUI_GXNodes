import { app } from "../../../scripts/app.js";
import { gxText } from "./gxi18n.js";

const oldTypes = {
  GXLLMV2: "GXLLMChatCompletions", GXAPIConfigV2: "GXAPIConfig",
  GXGenerationConfigV3: "GXGenerationConfig", GXSkillConfigV2: "GXSkillConfig", GXSkillCatalogV2: "GXSkillCatalog",
};
const supported = new Set(["GXLLMChatCompletions", "GXGenerationConfig", "GXAPIConfig"]);
// Older frontends can create dynamic widgets without applying nodeDefs translations.
const labels = {
  source: ["配置来源", "Config source"],
  dsh_model: ["模型（本地配置）", "Model (local profile)"],
  base_url: ["API 地址", "API URL"],
  model: ["模型名称", "Model name"],
  api_key_env: ["API Key 环境变量", "API key environment variable"],
  settings: ["生成预设", "Generation preset"],
  max_tokens: ["最大输出 token", "Max output tokens"],
  temperature: ["随机程度", "Temperature"],
  top_p: ["采样范围（top_p）", "Top-p sampling"],
  reasoning_effort: ["推理强度", "Reasoning effort"],
  token_field: ["输出上限字段", "Output limit field"],
  presence_penalty: ["话题重复惩罚", "Presence penalty"],
  frequency_penalty: ["词频重复惩罚", "Frequency penalty"],
  api_seed_mode: ["发送模型种子", "Send model seed"],
  stop: ["停止词（每行一个）", "Stop sequences (one per line)"],
  response_format: ["响应格式", "Response format"],
  advanced_extra_body: ["扩展请求参数（JSON）", "Extra request fields (JSON)"],
};
const wrapped = new WeakSet();

export function applyLabels(node) {
  for (const widget of node.widgets || []) {
    const name = String(widget.name).split(".").at(-1);
    const label = Object.hasOwn(labels, name) ? labels[name] : null;
    if (label && (!widget.label || widget.label === widget.name || widget.label === name || label.includes(widget.label))) {
      widget.label = gxText(...label);
    }
    if (!["source", "settings"].includes(widget.name) || !widget.callback || wrapped.has(widget.callback)) continue;
    const callback = widget.callback;
    widget.callback = function() {
      const result = callback.apply(this, arguments);
      applyLabels(node);
      return result;
    };
    wrapped.add(widget.callback);
  }
}

export function legacyNames(type, values) {
  if (type === "GXLLMV2") return ["system", "prompt", "context", "execution_seed", "control_after_generate"];
  if (type === "GXGenerationConfigV3") return values[0] === "default" ? ["settings"] :
    values[0] === "openai-completions" ? ["settings", ...["temperature", "token_field", "max_tokens", "top_p", "presence_penalty", "frequency_penalty", "reasoning_effort", "api_seed_mode", "stop", "response_format", "advanced_extra_body"].map(name => `settings.${name}`)] : [];
  if (type === "GXAPIConfigV2") return values[0] === "dsh" ? ["source", "source.dsh_model", "vision", "timeout_seconds"] :
    values[0] === "custom" ? ["source", "source.base_url", "source.model", "source.api_key_env", "vision", "timeout_seconds"] : [];
  return [];
}

// Convert legacy positional data to the native format BEFORE core configure
// restores dynamic widgets. New workflows use native named serialization only.
export function prepareWorkflow(type, info) {
  if (info.widgets_values_named != null || !Array.isArray(info.widgets_values)) return info;
  let names = legacyNames(type, info.widgets_values);
  if (type === "GXLLMChatCompletions") names = ["prompt", "system", "context", "execution_seed", "control_after_generate"];
  if (type === "GXGenerationConfig") names = info.widgets_values[0] === "default" ? ["settings"] :
    ["settings", ...["max_tokens", "temperature", "top_p", "reasoning_effort", "token_field", "presence_penalty", "frequency_penalty", "api_seed_mode", "stop", "response_format", "advanced_extra_body"].map(name => `settings.${name}`)];
  if (type === "GXAPIConfig") names = legacyNames("GXAPIConfigV2", info.widgets_values);
  if (names.length !== info.widgets_values.length) {
    throw new Error(gxText("GX LLM 旧工作流参数数量不匹配，请保留原文件并检查节点版本。", "Legacy GX LLM inputs do not match. Keep the original workflow and check the node version."));
  }
  return {...info, widgets_values_named: Object.fromEntries(names.map((name, i) => [name, info.widgets_values[i]]))};
}

export function cleanObsoleteSocket(node) {
  if (!["GXLLMChatCompletions", "GXLLMV2"].includes(node.comfyClass || node.type)) return;
  const index = node.inputs?.findIndex(input => input.name === "api_config" && input.link == null);
  if (index >= 0) node.removeInput(index);
}

export function migrateGraph(graph) {
  const migrateSlots = owner => {
    for (const slot of [...(owner?.inputs || []), ...(owner?.outputs || [])]) {
      if (slot.type === "GX_API_CONFIG_V2") slot.type = "GX_API_CONFIG";
    }
  };
  migrateSlots(graph);
  migrateSlots(graph.inputNode);
  migrateSlots(graph.outputNode);
  // Subgraph definitions carry the same node/link serialization as the root.
  for (const node of graph.nodes || []) {
    const old = node.type;
    if (oldTypes[old]) {
      if (["GXLLMV2", "GXAPIConfigV2", "GXGenerationConfigV3"].includes(old)) Object.assign(node, prepareWorkflow(old, node));
      node.type = oldTypes[old];
      if (old === "GXLLMV2" && (!node.title || node.title === "GX LLM")) node.title = "GX LLM Chat Completions";
    }
    migrateSlots(node);
  }
  for (const link of graph.links || []) {
    if (Array.isArray(link) && link[5] === "GX_API_CONFIG_V2") link[5] = "GX_API_CONFIG";
    else if (link?.type === "GX_API_CONFIG_V2") link.type = "GX_API_CONFIG";
  }
  for (const subgraph of graph.definitions?.subgraphs || []) migrateGraph(subgraph);
}

app.registerExtension({
  name: "gxnodes.llm-layout",
  beforeConfigureGraph(graph) { migrateGraph(graph); },
  beforeRegisterNodeDef(_nodeType, nodeData) {
    if (!supported.has(nodeData.name)) return;
    // Enable the core named restoration path even when the global flag is off.
    nodeData.fallbackWidgetsValuesNames = [];
  },
  nodeCreated(node) {
    const type = node.comfyClass || node.type;
    if (!supported.has(type)) return;
    applyLabels(node);

    const configure = node.configure;
    node.configure = function (info, ...args) {
      return configure.call(this, prepareWorkflow(type, info), ...args);
    };
    const configured = node.onConfigure;
    node.onConfigure = function (...args) {
      const result = configured?.apply(this, args);
      cleanObsoleteSocket(this);
      applyLabels(this);
      return result;
    };
  },
});
