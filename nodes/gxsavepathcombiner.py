import json
import os
import time

import folder_paths
from comfy_api.latest import io


def load_config():
    try:
        config_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "config.json")
        with open(config_path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
        names = config.get("subfolder_names")
        if isinstance(names, list) and names and all(isinstance(name, str) for name in names):
            return names
    except (OSError, ValueError) as error:
        print(f"加载配置文件失败: {error}")
    return ["None"]


def clean_segment(value):
    segment = str(value or "").strip().replace("\\", "/").strip("/")
    if not segment or segment == "None":
        return ""
    if "/" in segment or segment in (".", ".."):
        raise ValueError(f"子文件夹名称无效：{value}")
    return segment


class GXSavePathCombiner(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        names = load_config()
        return io.Schema(node_id="GXSavePathCombiner", display_name="📂 Save Path Combiner", category="GXNodes/Legacy", is_deprecated=True, inputs=[
            io.Combo.Input("SubfolderMode", ["Default", "Date"], default="Default"),
            io.Combo.Input("SubfolderName1", names, default=names[0]),
            io.Combo.Input("SubfolderName2", names, default=names[0]),
            io.String.Input("filename_prefix", default="ComfyUI"),
        ], outputs=[io.String.Output("STRING")])

    @classmethod
    def execute(cls, SubfolderMode="Default", SubfolderName1="None", SubfolderName2="None", filename_prefix="ComfyUI"):
        parts = [time.strftime("%Y-%m-%d", time.localtime())] if SubfolderMode == "Date" else []
        parts.extend(segment for segment in (clean_segment(SubfolderName1), clean_segment(SubfolderName2)) if segment)
        parts.append(str(filename_prefix).strip() or "ComfyUI")
        return io.NodeOutput(os.path.join(folder_paths.get_output_directory(), *parts))


V3_NODE_CLASSES = [GXSavePathCombiner]
