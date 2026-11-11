import torch
import torch.nn.functional as F

from comfy_api.latest import io


class GXHighPassSharpening(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="GXHighPassSharpening", display_name="🖼️ High Pass Sharpening", category="GXNodes/Legacy", is_deprecated=True, inputs=[
            io.Image.Input("image"),
            io.Float.Input("radius", default=2.0, min=1.0, max=50.0, step=1.0),
            io.Float.Input("intensity", default=1.0, min=0.0, max=3.0, step=0.01),
            io.Combo.Input("blend_mode", ["hard_light", "soft_light"], default="soft_light"),
            io.Float.Input("opacity", default=0.6, min=0.0, max=1.0, step=0.01),
        ], outputs=[io.Image.Output("IMAGE")])

    @classmethod
    def execute(cls, image, radius, intensity, blend_mode, opacity):
        size = max(int(radius * 2), 3)
        size += size % 2 == 0
        axis = torch.arange(-(size // 2), size // 2 + 1, device=image.device, dtype=image.dtype)
        gaussian = torch.exp(-(axis ** 2) / (2 * radius ** 2))
        kernel = (gaussian[:, None] * gaussian[None, :]) / (gaussian.sum() ** 2)
        nchw = image.permute(0, 3, 1, 2)
        channels = nchw.shape[1]
        kernel = kernel.expand(channels, 1, size, size)
        blurred = F.conv2d(nchw, kernel, padding=size // 2, groups=channels)
        blend = ((nchw - blurred) * intensity + 0.5).clamp(0, 1)
        base = nchw.clamp(0, 1)
        if blend_mode == "hard_light":
            mixed = torch.where(blend <= 0.5, 2 * base * blend, 1 - 2 * (1 - base) * (1 - blend))
        else:
            mixed = torch.where(blend <= 0.5, base * (2 * blend + base * (1 - 2 * blend)), base * (2 * (1 - blend) + torch.sqrt(base) * (2 * blend - 1)))
        return io.NodeOutput((base * (1 - opacity) + mixed * opacity).permute(0, 2, 3, 1).clamp(0, 1))


V3_NODE_CLASSES = [GXHighPassSharpening]
