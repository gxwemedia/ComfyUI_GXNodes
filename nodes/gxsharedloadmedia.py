import os
from comfy_api.latest import io, InputImpl


def _shared_result_path(reference):
    """Resolve only files returned by GXShared under the local output directory."""
    import folder_paths

    if not isinstance(reference, str) or not reference.endswith(" [output]"):
        raise ValueError("共享结果必须引用 output/GXShared 下的回传文件")

    relative_path = reference[:-len(" [output]")].replace("\\", "/")
    if not relative_path.startswith("GXShared/") or "/../" in f"/{relative_path}/":
        raise ValueError("共享结果路径无效")

    path = folder_paths.get_annotated_filepath(reference)
    output_root = os.path.realpath(folder_paths.get_output_directory())
    resolved_path = os.path.realpath(path)
    if os.path.commonpath((output_root, resolved_path)) != output_root or not os.path.isfile(resolved_path):
        raise ValueError("共享结果文件不存在或不在 output/GXShared 目录中")
    return resolved_path


class GXSharedLoadImage(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="GXSharedLoadImage", display_name="GX Shared Load Image", category="GXNodes/Shared", inputs=[io.String.Input("result", default="GXShared/ [output]")], outputs=[io.Image.Output("image"), io.Mask.Output("mask")])

    @classmethod
    def execute(cls, result):
        import nodes
        _shared_result_path(result)
        image, mask = nodes.LoadImage().load_image(result)
        return io.NodeOutput(image, mask)


class GXSharedLoadAudio(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="GXSharedLoadAudio", display_name="GX Shared Load Audio", category="GXNodes/Shared", inputs=[io.String.Input("result", default="GXShared/ [output]")], outputs=[io.Audio.Output("audio")])

    @classmethod
    def execute(cls, result):
        import torchaudio

        waveform, sample_rate = torchaudio.load(_shared_result_path(result))
        return io.NodeOutput({"waveform": waveform.unsqueeze(0), "sample_rate": sample_rate})


class GXSharedLoadVideo(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="GXSharedLoadVideo", display_name="GX Shared Load Video", category="GXNodes/Shared", inputs=[io.String.Input("result", default="GXShared/ [output]")], outputs=[io.Video.Output("video")])

    @classmethod
    def execute(cls, result):
        return io.NodeOutput(InputImpl.VideoFromFile(_shared_result_path(result)))


V3_NODE_CLASSES = [GXSharedLoadImage, GXSharedLoadAudio, GXSharedLoadVideo]
