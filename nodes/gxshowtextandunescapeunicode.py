import re

from comfy_api.latest import io


class GXShowTextAndUnescapeUnicode(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="GXShowTextAndUnescapeUnicode", display_name="📃 Show Text and Unescape Unicode", category="GXNodes/Legacy", is_deprecated=True, is_output_node=True, inputs=[io.String.Input("text", force_input=True)], outputs=[io.String.Output("STRING")], hidden=[io.Hidden.unique_id, io.Hidden.extra_pnginfo])

    @classmethod
    def execute(cls, text, unique_id=None, extra_pnginfo=None):
        values = text if isinstance(text, list) else [text]
        processed = []
        for value in values:
            value = "" if value is None else str(value)
            if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            value = value.replace("\\n", "\n")
            value = re.sub(r"\\\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), value)
            value = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), value)
            processed.append(value)
        output = "\n\n".join(processed)
        workflow = extra_pnginfo.get("workflow") if isinstance(extra_pnginfo, dict) else None
        if unique_id is not None and isinstance(workflow, dict):
            node_id = str(unique_id[0]) if isinstance(unique_id, list) and unique_id else str(unique_id)
            for node in workflow.get("nodes", []):
                if str(node.get("id", "")) == node_id:
                    node["widgets_values"] = [output]
                    break
        return io.NodeOutput(output, ui={"text": [[output]]})


V3_NODE_CLASSES = [GXShowTextAndUnescapeUnicode]
