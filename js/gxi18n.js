import { app } from "../../../scripts/app.js";

// Node definitions and settings are localized by ComfyUI. Custom DOM uses
// the same language setting through the extension manager's public API.
export function gxText(zh, en) {
  const locale = app.extensionManager?.setting?.get?.("Comfy.Locale") ?? document.documentElement.lang;
  return /^zh(?:[-_]|$)/i.test(locale) ? zh : en;
}
