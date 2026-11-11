import json
import os
import re
from pathlib import Path

import folder_paths
import numpy as np
from comfy_api.latest import io
from PIL import Image
from PIL.PngImagePlugin import PngInfo

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


def _output_root():
    return Path(folder_paths.get_output_directory()).resolve()


def _is_within(directory, candidate):
    try:
        return Path(candidate).resolve().is_relative_to(Path(directory).resolve())
    except (OSError, ValueError):
        return False


def _resolve_output_dir(value):
    root = _output_root()
    raw = Path(str(value).strip() or root)
    if not raw.is_absolute():
        raw = root / raw
    resolved = raw.resolve()
    if not _is_within(root, resolved):
        raise ValueError("output_dir 必须位于 ComfyUI output 目录内")
    return resolved


def _safe_filename(value):
    name = str(value).strip()
    if not name or name in (".", ".."):
        raise ValueError("文件名不能为空")
    if Path(name).is_absolute() or Path(name).name != name:
        raise ValueError(f"文件名不能包含路径：{name}")
    if "/" in name or "\\" in name:
        raise ValueError(f"文件名不能包含路径分隔符：{name}")
    if re.search(r'[<>:"|?*\x00-\x1f]', name):
        raise ValueError(f"文件名包含非法字符：{name}")
    if name != name.rstrip(" ."):
        raise ValueError(f"文件名不能以空格或句点结尾：{name}")
    if not Path(name).suffix:
        name += ".png"
    if Path(name).stem.lower() in _WINDOWS_RESERVED:
        raise ValueError(f"文件名是系统保留名称：{name}")
    if Path(name).suffix.lower() not in _IMAGE_SUFFIXES:
        name = str(Path(name).with_suffix(".png"))
    return name


def _unique_path(folder, name):
    candidate = folder / name
    stem, suffix = candidate.stem, candidate.suffix
    index = 1
    while candidate.exists():
        candidate = folder / f"{stem}_{index:03d}{suffix}"
        index += 1
    return candidate


def _metadata_for(path, prompt, extra_pnginfo):
    if path.suffix.lower() != ".png":
        return None
    from comfy.cli_args import args

    if args.disable_metadata:
        return None
    info = PngInfo()
    if prompt is not None:
        info.add_text("prompt", json.dumps(prompt))
    if isinstance(extra_pnginfo, dict):
        for key, value in extra_pnginfo.items():
            info.add_text(str(key), json.dumps(value))
    return info


class GXSaveImagesByName(io.ComfyNode):

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="GXSaveImagesByName",
            display_name="GX Save Images By Name",
            category="GXNodes/Save",
            description="Save an image batch using one filename per image. Files are saved under the ComfyUI output directory.",
            is_output_node=True,
            inputs=[
                io.Image.Input("images", tooltip="Images to save in the same order as the filename list."),
                io.String.Input("filenames", multiline=True, tooltip="Enter one filename per image, or connect a list from a loop. Use names without directory separators. Existing files receive a numeric suffix."),
                io.String.Input("output_dir", default=folder_paths.get_output_directory(), tooltip="Choose a folder under ComfyUI output. A relative path is resolved from output."),
            ],
            outputs=[],
            hidden=[io.Hidden.prompt, io.Hidden.extra_pnginfo],
        )

    @classmethod
    def execute(cls, images, filenames, output_dir, prompt=None, extra_pnginfo=None):
        target_dir = _resolve_output_dir(output_dir)
        if not _is_within(_output_root(), target_dir):
            raise ValueError("output_dir 必须位于 ComfyUI output 目录内")

        if isinstance(filenames, str):
            filename_list = [line.strip() for line in filenames.splitlines() if line.strip()]
        elif isinstance(filenames, (tuple, list)):
            filename_list = [str(name).strip() for name in filenames if str(name).strip()]
        else:
            raise ValueError(f"Unexpected filenames type: {type(filenames)}")

        if images.ndim == 3:
            images = images.unsqueeze(0)
        if len(images) != len(filename_list):
            raise ValueError(f"Number of images ({len(images)}) and filenames ({len(filename_list)}) do not match.")

        subfolder = target_dir.relative_to(_output_root()).as_posix()
        if subfolder == ".":
            subfolder = ""

        names = [_safe_filename(name) for name in filename_list]
        target_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for image, name in zip(images, names):
            destination = _unique_path(target_dir, name)
            pixels = 255.0 * image.cpu().numpy()
            img = Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))
            if destination.suffix.lower() in {".jpg", ".jpeg"} and img.mode == "RGBA":
                img = img.convert("RGB")
            metadata = _metadata_for(destination, prompt, extra_pnginfo)
            if metadata is not None:
                img.save(destination, pnginfo=metadata)
            else:
                img.save(destination)
            results.append({
                "filename": destination.name,
                "subfolder": subfolder,
                "type": "output",
            })

        return io.NodeOutput(ui={"images": results})


V3_NODE_CLASSES = [GXSaveImagesByName]
