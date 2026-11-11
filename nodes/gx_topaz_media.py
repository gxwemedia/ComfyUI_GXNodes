"""帧序列（IMAGE 张量） <-> 视频文件 的互转，基于 PyAV（ComfyUI 环境自带）。

只在节点层（有 torch / av 的运行时）被 import；纯标准库的 gx_topaz_ffmpeg 不依赖本
模块，因此验证脚本仍可在无 ComfyUI 的环境里跑命令构造与解析测试。

设计取舍（与用户选定的「IMAGE 帧序列为主」一致）：
- 输入 frames（IMAGE 批次）时，先用 PyAV 封装成一个临时视频再交给 Topaz 的 ffmpeg 处理；
- 输出先把 Topaz 结果视频解码回 IMAGE 批次，方便节点直接预览、也能接下游图像节点；
- 长视频解码会吃内存，故 video_to_images 有硬上限（默认 512 帧）保护，0 = 取上限。
"""

from __future__ import annotations

import numpy as np
import av
import torch
from fractions import Fraction

# IMAGE 张量的像素格式：ComfyUI 约定是 (B, H, W, C) 的 float，范围 0~1，RGB。
# PyAV 之间用 rgb24 中转最稳。


def images_to_video(
    frames: torch.Tensor,
    out_path: str,
    fps: float,
    pix_fmt: str = "yuv444p",
    codec: str = "libx264",
    crf: int = 0,
) -> None:
    """把 IMAGE 张量 (B, H, W, C)，范围 0~1，编码成一个视频文件。

    用于把上游给来的帧序列封装成 Topaz 可读的视频。默认 yuv444p + qp0 近无损，
    避免在处理前二次损伤画质；Topaz 自己还会再编码一次。
    """
    if frames.dim() != 4:
        raise ValueError(f"frames 必须是 (B,H,W,C)，实际 {tuple(frames.shape)}")
    b, h, w, c = frames.shape
    if c == 4:  # 丢弃 alpha，ffmpeg 编码器多数不吃 4 通道
        frames = frames[..., :3]
        c = 3
    if c != 3:
        raise ValueError(f"frames 通道数应为 3（或 4 带 alpha），实际 {c}")

    arr = (frames.clamp(0.0, 1.0).cpu().numpy() * 255.0).astype("uint8")  # (B,H,W,3) RGB

    container = av.open(out_path, mode="w")
    stream = container.add_stream(codec, rate=Fraction(fps).limit_denominator(1000))
    stream.width, stream.height = w, h
    stream.pix_fmt = pix_fmt if pix_fmt in ("yuv420p", "yuv444p", "yuv420p10le") else "yuv420p"
    # qp0 / crf 在部分 PyAV 版本不能直接赋值给 codec_context，走 codec 私有选项更稳
    if codec == "libx264":
        stream.codec_context.options["qp"] = "0" if crf == 0 else str(int(crf))

    for i in range(b):
        frame = av.VideoFrame.from_ndarray(arr[i], format="rgb24")
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode(None):  # flush
        container.mux(packet)
    container.close()


def video_to_images(video_path: str, max_frames: int = 0, hard_cap: int = 512) -> torch.Tensor:
    """把视频解码为 IMAGE 张量 (B, H, W, C)，范围 0~1。

    max_frames <= 0 时最多取 hard_cap 帧（防止长视频把内存撑爆）；
    > 0 时取 min(max_frames, hard_cap)。超出上限时按时间顺序取前 limit 帧
    （截断，不是均匀抽帧）。返回 (B,H,W,3) float32，范围 0~1。
    """
    container = av.open(video_path, mode="r")
    stream = container.streams.video[0]
    stream.thread_type = "AUTO"

    limit = hard_cap if max_frames <= 0 else min(int(max_frames), hard_cap)

    raw: list[np.ndarray] = []
    for frame in container.decode(video=0):
        raw.append(frame.to_ndarray(format="rgb24"))  # (H,W,3) uint8
        if len(raw) >= limit:
            break
    container.close()

    if not raw:
        raise RuntimeError(f"未能从视频解码出任何帧：{video_path}")

    # 预分配目标数组、逐帧就地缩放。旧写法 np.stack().astype()/255.0 会同时
    # 存在 uint8 副本 + float32 副本 + 除法结果三份全量数据：1080p×512 帧
    # 峰值差 8GB 以上。这里只多一份输出张量 + 单帧临时量，行为完全一致。
    out = np.empty((len(raw), *raw[0].shape), dtype=np.float32)
    for i, frame_arr in enumerate(raw):
        np.copyto(out[i], frame_arr, casting="unsafe")
        out[i] /= 255.0  # 与旧写法 astype(float32)/255.0 逐位一致
    return torch.from_numpy(out)
