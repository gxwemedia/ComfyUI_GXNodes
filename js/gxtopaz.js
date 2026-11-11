import { app } from "../../../scripts/app.js";

const topazNodes = new Set([
  "GX_TopazFrameInterpolation",
  "GX_TopazEnhancement",
  "GX_TopazVideoAI",
]);

const videoControls = new Set([
  "fi_model", "fi_slowmo", "fi_rdt", "fi_fps",
  "up_model", "up_width", "up_height", "up_scale",
  "up_preblur", "up_noise", "up_details", "up_halo", "up_blur", "up_compression",
  "up_model.up_blend", "up_blend", "up_add_noise",
]);

function hideInternalWidgets(node) {
  if (!topazNodes.has(node.comfyClass || node.type)) return;
  for (const widget of node.widgets || []) {
    if (!videoControls.has(widget.name)) {
      widget.hidden = true;
    } else if (widget.name === "fi_model" || widget.name === "up_model") {
      widget.options.getOptionLabel = value => value?.split(" · ", 1)[0] ?? "";
    }
  }
}

app.registerExtension({
  name: "gxnodes.topaz",
  nodeCreated: hideInternalWidgets,
  loadedGraphNode: hideInternalWidgets,
});
