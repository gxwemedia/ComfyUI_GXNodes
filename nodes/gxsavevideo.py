import os

from comfy.cli_args import args
from comfy_api.latest import io


class GXSaveVideo(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="GXSaveVideo", display_name="GX Save Video", category="GXNodes/Legacy", is_deprecated=True, is_output_node=True, inputs=[
            io.Video.Input("video"),
            io.String.Input("filename_prefix", default="video/ComfyUI"),
            io.Combo.Input("format", ["auto", "mp4", "mkv", "webm"], default="auto"),
            io.Combo.Input("codec", ["auto", "h264", "av1"], default="auto"),
            io.Float.Input("crf", default=23.0, min=0.0, max=51.0, step=1.0, optional=True),
        ], outputs=[io.Video.Output("video")], hidden=[io.Hidden.prompt, io.Hidden.extra_pnginfo])

    @classmethod
    def execute(cls, video, filename_prefix, format, codec, crf=23.0, prompt=None, extra_pnginfo=None):
        import folder_paths
        from comfy_api.latest import Types, ui

        if format == "webm" and codec == "h264":
            raise ValueError("WebM 容器不支持 H.264 编码，请改用 AV1 或 MP4")
        format_name = "webm" if format == "auto" and codec == "av1" else ("mp4" if format == "auto" else format)

        width, height = video.get_dimensions()
        full_output_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            filename_prefix,
            folder_paths.get_output_directory(),
            width,
            height,
        )
        os.makedirs(full_output_folder, exist_ok=True)
        extension = Types.VideoContainer.get_extension(format_name)
        output_file = f"{filename}_{counter:05}_.{extension}"

        metadata = None
        if not args.disable_metadata:
            metadata = {}
            if extra_pnginfo is not None:
                metadata.update(extra_pnginfo)
            if prompt is not None:
                metadata["prompt"] = prompt
            metadata = metadata or None

        video.save_to(
            os.path.join(full_output_folder, output_file),
            format=Types.VideoContainer(format_name),
            codec=Types.VideoCodec(codec),
            metadata=metadata,
            crf=crf,
        )
        preview = ui.PreviewVideo([
            ui.SavedResult(output_file, subfolder, ui.FolderType.output),
        ])
        return io.NodeOutput(video, ui=preview)


V3_NODE_CLASSES = [GXSaveVideo]
