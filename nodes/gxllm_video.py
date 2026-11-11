"""Bounded CPU-only frame sampling for native ComfyUI VIDEO sources."""

import io
import math
from pathlib import Path
from .gxllm_backend import check_interrupt

MAX_SECONDS = 60.0
MAX_DECODED_FRAMES = 3600
MAX_FRAME_PIXELS = 2048 * 2048
MAX_SOURCE_BYTES = 256 * 1024 * 1024


def _image_slots(images):
    count = 0
    for image in images:
        if len(image.shape) != 4 or image.shape[0] < 1:
            raise ValueError("IMAGE 批次格式无效。")
        count += int(image.shape[0])
    if count >= 8:
        raise ValueError("图片和视频抽帧合计最多 8 张；请为视频留出空间。")
    return min(4, 8 - count)


def sample_video(video, images, cancelled=None, *, with_times=False):
    def checkpoint():
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError("视频抽帧已取消。")
        check_interrupt()

    import av
    import numpy as np
    import torch
    from comfy_api.latest._input_impl.video_types import (
        VideoFromComponents,
        VideoFromFile,
    )

    slots = _image_slots(images)
    checkpoint()
    if isinstance(video, VideoFromComponents):
        parts = video.get_components()
        frames = parts.images
        if getattr(frames.device, "type", None) != "cpu":
            raise ValueError("视频帧必须在 CPU 上；请先提供 CPU 视频。")
        if (
            len(frames.shape) != 4
            or not frames.shape[0]
            or frames.shape[0] > MAX_DECODED_FRAMES
            or frames.shape[1] * frames.shape[2] > MAX_FRAME_PIXELS
        ):
            raise ValueError("视频帧尺寸无效或超过像素限制。")
        rate = float(parts.frame_rate)
        if not math.isfinite(rate) or rate <= 0 or frames.shape[0] / rate > MAX_SECONDS:
            raise ValueError("视频时长须在 60 秒以内。")
        count = min(slots, int(frames.shape[0]))
        indices = [
            round(i * (frames.shape[0] - 1) / max(count - 1, 1)) for i in range(count)
        ]
        out = []
        for index in indices:
            checkpoint()
            out.append(frames[index : index + 1].clone())
        return (out, [index / rate for index in indices]) if with_times else out
    if not isinstance(video, VideoFromFile):
        raise ValueError(
            "当前仅支持 ComfyUI 文件视频或 CPU 帧视频；请先转换为这两种 VIDEO。"
        )
    source = video.get_stream_source()
    if isinstance(source, str):
        path = Path(source)
        if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError("视频文件不可读或超过 256 MiB。")
    elif isinstance(source, io.BytesIO):
        data = source.getbuffer()
        if data.nbytes > 32 * 1024 * 1024:
            raise ValueError("内存视频数据超过 32 MiB。")
        source = io.BytesIO(data)
    else:
        raise ValueError("不支持该视频来源。")
    try:
        with av.open(source, mode="r") as container:
            if not container.streams.video:
                raise ValueError("视频没有图像流。")
            stream = container.streams.video[0]
            if (
                stream.width < 1
                or stream.height < 1
                or stream.width * stream.height > MAX_FRAME_PIXELS
            ):
                raise ValueError("视频帧超过像素限制。")
            if video.get_dimensions() != (stream.width, stream.height):
                raise ValueError("当前不支持已裁剪的视频；请先导出裁剪结果。")
            duration = (
                float(stream.duration * stream.time_base)
                if stream.duration is not None and stream.time_base is not None
                else float(container.duration / av.time_base)
                if container.duration is not None
                else 0
            )
            start, trim = video.get_active_trim_window()
            start = max(float(start), 0.0)
            end = min(duration, start + float(trim)) if trim else duration
            span = end - start
            if not math.isfinite(span) or span <= 0 or span > MAX_SECONDS:
                raise ValueError("视频时长须在 60 秒以内且可读取时长。")
            targets = [start + span * i / slots for i in range(slots)]
            out = []
            times = []
            decoded = 0
            origin = (
                float(stream.start_time * stream.time_base)
                if stream.start_time is not None and stream.time_base is not None
                else 0.0
            )
            for frame in container.decode(stream):
                checkpoint()
                decoded += 1
                if decoded > MAX_DECODED_FRAMES:
                    raise ValueError("视频解码帧数超过 3600，请先缩短视频。")
                time = float(frame.time) - origin if frame.time is not None else None
                if time is None:
                    raise ValueError("视频缺少帧时间信息。")
                if time >= end:
                    break
                if time < targets[len(out)]:
                    continue
                arr = frame.to_ndarray(format="rgb24")
                out.append(
                    torch.from_numpy(np.array(arr, copy=True))
                    .float()
                    .div_(255)
                    .unsqueeze(0)
                )
                times.append(time)
                if len(out) == slots:
                    break
            if not out:
                raise ValueError("未能从视频中读取帧。")
            return (out, times) if with_times else out
    except (OSError, av.error.FFmpegError):
        raise ValueError("视频无法解码；请使用可读取的本地视频。") from None
