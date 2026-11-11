import math

from comfy_api.latest import io


class GXOutpaintingSize(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="GXOutpaintingSize",
            display_name="🔍 Outpainting Size",
            category="GXNodes/Legacy", is_deprecated=True,
            is_output_node=True,
            inputs=[
                io.Image.Input("Image"),
                io.Int.Input("Width", default=1920, min=64, max=4096, step=8),
                io.Int.Input("Height", default=1080, min=64, max=4096, step=8),
                io.Int.Input("Horizontal_Offset", default=0, min=-2048, max=2048, step=8),
                io.Int.Input("Vertical_Offset", default=0, min=-2048, max=2048, step=8),
            ],
            outputs=[
                io.Image.Output("Image"),
                io.Int.Output("left"),
                io.Int.Output("top"),
                io.Int.Output("right"),
                io.Int.Output("bottom"),
            ],
        )

    @classmethod
    def execute(cls, Image, Width, Height, Horizontal_Offset, Vertical_Offset):
        image_width = Image.shape[2]
        image_height = Image.shape[1]
        base_horizontal = math.ceil((Width - image_width) / 2)
        base_vertical = math.ceil((Height - image_height) / 2)
        return io.NodeOutput(
            Image,
            base_horizontal + Horizontal_Offset,
            base_vertical + Vertical_Offset,
            base_horizontal - Horizontal_Offset,
            base_vertical - Vertical_Offset,
            ui={"size_widget": [f"{image_width}x{image_height}"]},
        )


V3_NODE_CLASSES = [GXOutpaintingSize]
