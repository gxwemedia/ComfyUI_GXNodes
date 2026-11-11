import json
import os
import time

import folder_paths
import numpy as np
from comfy.cli_args import args
from comfy_api.latest import io
from PIL import Image
from PIL.PngImagePlugin import PngInfo

PNG_COMPRESS_LEVEL = 4


def _join_prefix(*parts):
    cleaned = []
    for part in parts:
        value = str(part or "").strip().strip("/\\")
        if value and value != "None":
            cleaned.append(value)
    return "/".join(cleaned)


class GXSaveImagePlus(io.ComfyNode):
    @classmethod
    def load_config(cls):
        """加载配置文件"""
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

    @classmethod
    def define_schema(cls):
        subfolder_names = cls.load_config()
        return io.Schema(
            node_id="GXSaveImagePlus",
            display_name="Save Image Plus",
            category="GXNodes/Legacy", is_deprecated=True,
            description="增强型图像保存节点，支持日期目录、双层子文件夹和 PNG 元数据。",
            is_output_node=True,
            inputs=[
                io.Image.Input("images"),
                io.Combo.Input("SaveMode", ["Date", "Default"], default="Date"),
                io.Combo.Input("SubfolderName1", subfolder_names, default=subfolder_names[0]),
                io.Combo.Input("SubfolderName2", subfolder_names, default=subfolder_names[0]),
                io.String.Input("filename_prefix", default="ComfyUI"),
            ],
            outputs=[],
            hidden=[io.Hidden.prompt, io.Hidden.extra_pnginfo],
        )

    @classmethod
    def execute(cls, images, SaveMode, SubfolderName1, SubfolderName2, filename_prefix, prompt=None, extra_pnginfo=None):
        first = "" if SubfolderName1 == "None" else SubfolderName1
        second = "" if SubfolderName2 == "None" else SubfolderName2
        date_folder = time.strftime("%Y-%m-%d", time.localtime()) if SaveMode == "Date" else ""
        prefix = _join_prefix(date_folder, first, second, filename_prefix)

        output_dir = folder_paths.get_output_directory()
        full_output_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            prefix, output_dir, images[0].shape[1], images[0].shape[0]
        )
        os.makedirs(full_output_folder, exist_ok=True)

        results = []
        for batch_number, image in enumerate(images):
            pixels = 255.0 * image.cpu().numpy()
            img = Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))

            metadata = None
            if not args.disable_metadata:
                metadata = PngInfo()
                if prompt is not None:
                    metadata.add_text("prompt", json.dumps(prompt))
                if isinstance(extra_pnginfo, dict):
                    for key, value in extra_pnginfo.items():
                        metadata.add_text(str(key), json.dumps(value))

            filename_with_batch_num = filename.replace("%batch_num%", str(batch_number))
            file = f"{filename_with_batch_num}_{counter:05}_.png"
            img.save(os.path.join(full_output_folder, file), pnginfo=metadata, compress_level=PNG_COMPRESS_LEVEL)

            results.append({"filename": file, "subfolder": subfolder, "type": "output"})
            counter += 1

        return io.NodeOutput(ui={"images": results})


V3_NODE_CLASSES = [GXSaveImagePlus]
