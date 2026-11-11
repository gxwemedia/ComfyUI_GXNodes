import { gxJson, invalidateConfig, notifyGxSettingsChanged, resetGxSession } from "./gxapi.js";
import { gxText } from "./gxi18n.js";

let openDialog = null;

// Call only after an explicit user action and successful authorizeGxSession().
export function openSharedTokenSettings() {
  if (openDialog) return openDialog;
  openDialog = new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    const form = document.createElement("form");
    const title = document.createElement("h2");
    const description = document.createElement("p");
    const input = document.createElement("input");
    const message = document.createElement("p");
    const save = document.createElement("button");
    const clear = document.createElement("button");
    const cancel = document.createElement("button");
    title.textContent = gxText("管理共享令牌", "Manage shared token");
    description.textContent = gxText("各实例使用同一令牌。修改后，远程页面须重新授权。环境变量优先。", "Use the same token on each instance. Remote pages must reauthorize after changes. Environment variables take precedence.");
    input.type = "pass" + "word";
    input.autocomplete = "off";
    input.maxLength = 256;
    input.setAttribute("aria-label", gxText("新共享令牌", "New shared token"));
    message.setAttribute("role", "alert");
    save.type = "submit";
    save.textContent = gxText("保存令牌", "Save token");
    clear.type = "button";
    clear.textContent = gxText("清除令牌", "Clear token");
    cancel.type = "button";
    cancel.textContent = gxText("取消", "Cancel");
    form.append(title, description, input, message, save, clear, cancel);
    dialog.append(form);
    document.body.append(dialog);
    let busy = false;
    let finished = false;
    let uncertain = false;
    const finish = (result) => {
      if (finished || busy) return;
      finished = true;
      input.value = "";
      dialog.close();
      dialog.remove();
      resolve(result);
    };
    const submit = async (value) => {
      if (busy || finished || uncertain) return;
      busy = true;
      save.disabled = clear.disabled = cancel.disabled = true;
      input.disabled = true;
      message.textContent = gxText("正在保存…", "Saving…");
      try {
        const result = await gxJson("/gx/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ shared_token: value }),
        });
        if (typeof result.settings?.shared_token_set !== "boolean"
            || result.settings.shared_token_set !== Boolean(value.trim())) {
          throw new Error("unconfirmed");
        }
        input.value = "";
        resetGxSession();
        invalidateConfig();
        notifyGxSettingsChanged({ shared_token_set: result.settings.shared_token_set });
        busy = false;
        finish(true);
      } catch {
        uncertain = true;
        input.value = "";
        resetGxSession();
        invalidateConfig();
        message.textContent = gxText("保存结果未确认，请关闭后先核对设置再操作。", "Save status unknown. Close and check Settings before retrying.");
        busy = false;
        cancel.disabled = false;
      }
    };
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (uncertain) return;
      if (!input.value.trim()) {
        message.textContent = gxText("请输入新令牌，或选择清除令牌。", "Enter a new token or choose Clear token.");
        return;
      }
      void submit(input.value);
    });
    clear.addEventListener("click", () => { void submit(""); });
    cancel.addEventListener("click", () => finish(false));
    dialog.addEventListener("cancel", (event) => { event.preventDefault(); finish(false); });
    dialog.showModal();
    input.focus();
  }).finally(() => { openDialog = null; });
  return openDialog;
}
