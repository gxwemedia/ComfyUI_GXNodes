from comfy_api.latest import io


class GXStringLiteral(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="GXStringLiteral",
            display_name="📃 String Literal",
            category="GXNodes/Legacy", is_deprecated=True,
            description="提供一个可多行编辑的字符串常量。",
            inputs=[io.String.Input("String", multiline=True)],
            outputs=[io.String.Output("STRING")],
        )

    @classmethod
    def execute(cls, String):
        return io.NodeOutput(String)


V3_NODE_CLASSES = [GXStringLiteral]
