import torch

from comfy_api.latest import io


class GXVideoReverse(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="GXVideoReverse",
            display_name="📽️ Video Reverse",
            category="GXNodes/Legacy", is_deprecated=True,
            inputs=[
                io.Image.Input("images"),
                io.Float.Input("FPS", default=30.0, min=1.0, max=60.0, step=0.1),
            ],
            outputs=[
                io.Image.Output("reversed_images"),
                io.Int.Output("frame_count"),
                io.Float.Output("duration_seconds"),
            ],
        )

    @classmethod
    def execute(cls, images, FPS=30.0):
        if images is None or not isinstance(images, torch.Tensor):
            raise ValueError("输入必须是非空的 PyTorch IMAGE 张量")
        if images.numel() == 0 or images.shape[0] == 0:
            return io.NodeOutput(images, 0, 0.0)
        reversed_images = torch.flip(images, dims=(0,)).clamp(0.0, 1.0)
        if reversed_images.dtype != torch.float32:
            reversed_images = reversed_images.to(torch.float32)
        frame_count = reversed_images.shape[0]
        return io.NodeOutput(reversed_images, frame_count, round(frame_count / max(FPS, 0.1), 2))


V3_NODE_CLASSES = [GXVideoReverse]
