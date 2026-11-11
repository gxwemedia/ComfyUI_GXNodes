// Shared HTTP helpers. Tokens exist only in this page's memory.
import { gxText } from "./gxi18n.js";

let tokenPromise = null;
let currentToken = "";
let authorizationPromise = null;
let authorizationVersion = 0;

async function loadToken(force = false) {
  if (!tokenPromise || force) {
    const version = authorizationVersion;
    const pending = fetch("/gx/session", { cache: "no-store", credentials: "same-origin" })
      .then(async (response) => {
        if (!response.ok) return null;
        const body = await response.json();
        if (version !== authorizationVersion || tokenPromise !== pending) return null;
        if (typeof body?.token === "string") {
          currentToken = body.token;
          authorizationVersion++;
        }
        return body;
      })
      .catch(() => null);
    tokenPromise = pending;
  }
  const pending = tokenPromise;
  const result = await pending;
  if (!result && tokenPromise === pending) tokenPromise = null;
  return result;
}

// Call only from an explicit user action. Concurrent callers share one dialog.
export function authorizeGxSession() {
  if (authorizationPromise) return authorizationPromise;
  authorizationPromise = (async () => {
    const session = await loadToken();
    if (!session) throw new Error(gxText("无法读取 GX 会话，请检查连接。", "Could not read the GX session. Check the connection."));
    if (typeof session.token === "string" || !session.required) return true;
    if (currentToken && session.authenticated) return true;

    const version = authorizationVersion;
    return new Promise((resolve) => {
      const dialog = document.createElement("dialog");
      const form = document.createElement("form");
      const title = document.createElement("p");
      const input = document.createElement("input");
      const message = document.createElement("p");
      const submit = document.createElement("button");
      const cancel = document.createElement("button");
      title.textContent = gxText("输入此实例的共享令牌", "Enter this instance's sharing token");
      input.type = "pass" + "word";
      input.autocomplete = "off";
      input.required = true;
      input.setAttribute("aria-label", gxText("共享令牌", "Sharing token"));
      message.setAttribute("role", "alert");
      submit.type = "submit";
      submit.textContent = gxText("授权", "Authorize");
      cancel.type = "button";
      cancel.textContent = gxText("取消", "Cancel");
      form.append(title, input, message, submit, cancel);
      dialog.append(form);
      document.body.append(dialog);
      let finished = false;
      const finish = (result) => {
        if (finished) return;
        finished = true;
        input.value = "";
        dialog.close();
        dialog.remove();
        resolve(result);
      };
      cancel.addEventListener("click", () => finish(false));
      dialog.addEventListener("cancel", (event) => { event.preventDefault(); finish(false); });
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (submit.disabled || !input.value) return;
        const candidate = input.value;
        submit.disabled = true;
        message.textContent = gxText("验证中…", "Verifying…");
        try {
          const response = await fetch("/gx/session", {
            cache: "no-store", credentials: "same-origin", redirect: "error", headers: { "X-GX-Token": candidate },
          });
          const body = await response.json();
          if (finished) return;
          if (version !== authorizationVersion) { finish(false); return; }
          if (!response.ok) {
            message.textContent = body?.code === "cross_origin" ? gxText("请求被拒绝：不允许跨站调用", "Cross-origin request blocked") : gxText("会话验证失败，请重试。", "Authorization failed. Try again.");
          } else if (body.authenticated === true || typeof body.token === "string") {
            currentToken = typeof body.token === "string" ? body.token : candidate;
            authorizationVersion++;
            tokenPromise = Promise.resolve(body);
            invalidateConfig();
            finish(true);
            return;
          } else {
            message.textContent = gxText("令牌无效，请重新输入。", "Invalid token. Try again.");
          }
        } catch {
          if (finished) return;
          message.textContent = gxText("无法验证令牌，请检查连接。", "Could not verify token. Check the connection.");
        }
        submit.disabled = false;
      });
      dialog.showModal();
      input.focus();
    });
  })().finally(() => { authorizationPromise = null; });
  return authorizationPromise;
}

export function resetGxSession() {
  authorizationVersion++;
  currentToken = "";
  tokenPromise = null;
  invalidateConfig();
}

function waitForSession(signal) {
  if (!signal) return loadToken();
  if (signal.aborted) return Promise.reject(signal.reason);
  return new Promise((resolve, reject) => {
    const abort = () => { cleanup(); reject(signal.reason); };
    const cleanup = () => signal.removeEventListener("abort", abort);
    signal.addEventListener("abort", abort, { once: true });
    loadToken().then((result) => { cleanup(); resolve(result); }, (error) => { cleanup(); reject(error); });
  });
}

export async function gxFetch(url, options = {}) {
  await waitForSession(options.signal);
  if (options.signal?.aborted) throw options.signal.reason;
  const headers = new Headers(options.headers || {});
  const target = new URL(typeof url === "string" ? url : url.url || url, globalThis.location.href);
  const sentToken = currentToken;
  const sentVersion = authorizationVersion;
  const sameOrigin = target.origin === globalThis.location.origin;
  if (sentToken && sameOrigin) headers.set("X-GX-Token", sentToken);
  const response = await fetch(url, { ...options, headers, ...(sentToken && sameOrigin ? { redirect: "error" } : {}) });
  if (response.status === 403) {
    const body = await response.clone().json().catch(() => ({}));
    if (body?.code === "gx_auth_required" && sameOrigin && sentToken && authorizationVersion === sentVersion) {
      resetGxSession();
    }
  }
  return response;
}

export async function gxJson(url, options = {}) {
  const response = await gxFetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok || body?.ok === false) {
    throw new Error(body?.error || body?.detail || gxText(`请求失败 HTTP ${response.status}`, `Request failed (HTTP ${response.status})`));
  }
  return body;
}

let configPromise = null;

export async function gxConfig(force = false) {
  if (!configPromise || force) {
    configPromise = gxFetch("/gx/config", { cache: "no-store" })
      .then((response) => (response.ok ? response.json() : {}))
      .catch(() => ({}));
  }
  return configPromise;
}

export function invalidateConfig() {
  configPromise = null;
}

const listeners = new Set();

export function onGxSettingsChanged(handler) {
  listeners.add(handler);
  return () => listeners.delete(handler);
}

export function notifyGxSettingsChanged(patch) {
  for (const handler of [...listeners]) {
    try {
      handler(patch || {});
    } catch (error) {
      console.error("[GXNodes] settings listener failed", error);
    }
  }
}
