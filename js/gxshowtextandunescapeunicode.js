import { app } from "../../../scripts/app.js";
import { ComfyWidgets } from "../../../scripts/widgets.js";
import { gxText } from "./gxi18n.js";

function decodeText(value) {
  let text = String(value);
  if (text.startsWith('"') && text.endsWith('"')) text = text.slice(1, -1);
  return text.replace(/\\n/g, "\n").replace(/\\u([0-9a-fA-F]{4})/g, (_, code) =>
    String.fromCharCode(Number.parseInt(code, 16)),
  );
}

function populate(node, text) {
  // Preserve the hidden input widget in older serialized workflows.
  const firstDisplayWidget = Number(Boolean(node.inputs?.[0]?.widget));
  for (const widget of node.widgets?.slice(firstDisplayWidget) ?? []) {
    node.removeWidget(widget);
  }

  let values = Array.isArray(text) ? text : [text];
  if (values.length === 1 && Array.isArray(values[0])) values = values[0];
  for (const value of values) {
    const name = `text_${node.widgets?.length ?? 0}`;
    const { widget } = ComfyWidgets.STRING(node, name, ["STRING", { multiline: true }], app);
    widget.label = gxText("文本", "Text");
    widget.inputEl.readOnly = true;
    widget.inputEl.style.opacity = 0.6;
    widget.inputEl.style.minHeight = "30px";
    widget.value = decodeText(value);
  }

  requestAnimationFrame(() => {
    const size = node.computeSize();
    size[0] = Math.max(size[0], node.size[0]);
    size[1] = Math.max(size[1], node.size[1]);
    node.onResize?.(size);
    node.setDirtyCanvas?.(true, false);
  });
}

app.registerExtension({
  name: "gxnodes.ShowTextAndUnescapeUnicode",
  nodeCreated(node) {
    if ((node.comfyClass || node.type) !== "GXShowTextAndUnescapeUnicode") return;

    const onExecuted = node.onExecuted;
    node.onExecuted = function (message) {
      const result = onExecuted?.apply(this, arguments);
      if (message?.text != null) populate(this, message.text);
      return result;
    };

    // Preserve legacy positional display values before the host restores widgets.
    const configure = node.configure;
    let savedValues;
    node.configure = function (info, ...args) {
      savedValues = info?.widgets_values;
      return configure.call(this, info, ...args);
    };

    const onConfigure = node.onConfigure;
    node.onConfigure = function (...args) {
      const result = onConfigure?.apply(this, args);
      if (savedValues?.length) {
        const values = savedValues;
        requestAnimationFrame(() => {
          const start = Number(values.length > 1 && Boolean(this.inputs?.[0]?.widget));
          populate(this, values.slice(start));
        });
      }
      return result;
    };
  },
});
