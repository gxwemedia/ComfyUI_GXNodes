import { app } from "../../../scripts/app.js";
import { gxText } from "./gxi18n.js";

app.registerExtension({
  name: "gxnodes.gxpadoutpaintingsize",
  nodeCreated(node) {
    if ((node.comfyClass || node.type) !== "GXOutpaintingSize") return;

    const widget = node.addWidget("text", "Original Size", "", () => {}, {
      multiline: false,
      readonly: true,
      disabled: true,
    });
    widget.label = gxText("原始尺寸", "Original Size");

    const onExecuted = node.onExecuted;
    node.onExecuted = function (message) {
      const result = onExecuted?.apply(this, arguments);
      if (message?.size_widget) {
        const size = message.size_widget;
        widget.value = (Array.isArray(size) ? size.join("") : String(size)).replace("x", "*");
        this.setDirtyCanvas?.(true, true);
      }
      return result;
    };
  },
});
