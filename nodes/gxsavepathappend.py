import os
import time

import folder_paths
from comfy_api.latest import io

try:
    from .gxsavepathcombiner import clean_segment, load_config
except ImportError:
    from gxsavepathcombiner import clean_segment, load_config


class GXSavePathAppend(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        names = load_config()
        return io.Schema(node_id="GXSavePathAppend", display_name="➕ Save Path Append", category="GXNodes/Legacy", is_deprecated=True, inputs=[
            io.String.Input("base_path", default=""),
            io.Combo.Input("SubfolderMode", ["Default", "Date"], default="Default"),
            io.Combo.Input("SubfolderName1", names, default=names[0]),
            io.Combo.Input("SubfolderName2", names, default=names[0]),
            io.String.Input("filename_prefix", default="ComfyUI"),
        ], outputs=[io.String.Output("STRING")])

    @classmethod
    def execute(cls, base_path="", SubfolderMode="Default", SubfolderName1="None", SubfolderName2="None", filename_prefix="ComfyUI"):
        base = str(base_path or "").strip() or folder_paths.get_output_directory()
        parts = [base]
        if SubfolderMode == "Date":
            parts.append(time.strftime("%Y-%m-%d", time.localtime()))
        parts.extend(segment for segment in (clean_segment(SubfolderName1), clean_segment(SubfolderName2)) if segment)
        parts.append(str(filename_prefix).strip() or "ComfyUI")
        return io.NodeOutput(os.path.join(*parts))


V3_NODE_CLASSES = [GXSavePathAppend]
