"""Topaz Video AI ffmpeg command builder / runner.

纯标准库实现，不 import 任何 ComfyUI 模块，因此可以脱离 ComfyUI 单独跑验证脚本。

参数与取值以 Topaz Video AI 6.2.1 导出的 FFmpeg 命令为依据。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import random
import re
import subprocess
import sys
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

# Windows 下 ffmpeg 是控制台子系统程序，直接 subprocess 会弹一个黑窗口。
# CREATE_NO_WINDOW 让子进程不分配可见控制台，进度走 stderr 管道由我们解析。
_EXEC_KW: dict = {}
if sys.platform == "win32":
    _EXEC_KW["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

# ffmpeg 进度行形如：
#   frame= 120 fps=45.0 q=18.0 size=1024kB time=00:00:04.00 bitrate=2048.0k speed=1.5x
# 开头还会打印一次：  Duration: 00:05:12.34, ...
_TIME_RE = re.compile(r"time=(\d+:\d+:\d+\.\d+)")
_FRAME_RE = re.compile(r"frame=\s*(\d+)")
_SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")
_DUR_RE = re.compile(r"Duration:\s*(\d+:\d+:\d+\.\d+)")

# ---------------------------------------------------------------------------
# 文档中的固定项
# ---------------------------------------------------------------------------

SWS_FLAGS = "spline+accurate_rnd+full_chroma_int"
MOV_FLAGS = "frag_keyframe+empty_moov+delay_moov+use_metadata_tags+write_colr"

FI_MODELS = ("chf-3", "chr-2", "apo-8")
UP_MODELS = ("rhea-1", "prob-4")  # rhea-1 = Rhea, prob-4 = Proteus

# 「Enhanced using ...」这一长串文案在文档 4 条命令中完全一致
ENHANCE_META = (
    "Enhanced using {model}; mode: auto; revert compression at 0; "
    "recover details at 0; sharpen at 0; reduce noise at 0; dehalo at 0; "
    "anti-alias/deblur at 0; focus fix Off; and recover original detail at 20"
)


def _normalize_path(value: str) -> str:
    """去掉路径首尾的空白与包裹引号（单/双）。

    用户常从终端拖拽文件、或从 LoadVideo 等节点拿到带引号的路径
    （如 '"D:/x.mp4"'）。subprocess 走参数列表（无 shell），这些引号会变成
    文件名里的字面字符，ffmpeg 就找不到文件（'Illegal byte sequence'）。
    所以凡是要交给 ffmpeg 的路径，都先过这一道。
    """
    v = (value or "").strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1].strip()
    elif len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        v = v[1:-1].strip()
    # 个别情况下复制出来是 shell 转义的 \"/' 开头
    if len(v) >= 2 and v[0] in ('"', "'") and v[-1] in ('"', "'"):
        v = v[1:-1].strip()
    return v


def _num(value: int | float | str) -> str:
    """按文档风格输出数字：整数值的 float 不带 .0，避免 '1.0' 与 '1' 不一致。"""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:g}"
    return str(value)


@dataclass
class TopazConfig:
    """一条 ffmpeg 命令的全部可变参数，默认值对应文档中最常用的一条命令。"""

    # 路径
    ffmpeg_path: str = "ffmpeg"
    input_path: str = ""
    output_path: str = ""  # 空 = 自动生成 <原名>_<9位数字>.mp4

    # tvai_fi 帧插值
    enable_interpolation: bool = True
    fi_model: str = "chf-3"
    fi_slowmo: float = 1.0
    fi_rdt: float = 0.02          # 对应 GUI 的 Sensitivity：20 -> 0.02，10 -> 0.01
    fi_fps: int = 30              # 0 = 不写入 fps 参数（文档第 3 条 chr-2 即如此）
    fi_device: int = -2
    fi_vram: float = 1.0
    fi_instances: int = 1

    # tvai_up 放大 / 质量提升
    enable_enhancement: bool = False
    up_model: str = "rhea-1"
    up_scale: float = 0.0         # 0 = 由 w/h 指定输出尺寸
    up_width: int = 2560
    up_height: int = 1440
    up_preblur: float = 0.0
    up_noise: float = 0.0
    up_details: float = 0.0
    up_halo: float = 0.0
    up_blur: float = 0.0
    up_compression: float = 0.0
    up_add_noise: int = 0
    up_estimate: int = 8
    up_blend: float = 0.2         # 仅文档中的 prob-4 命令带此参数
    use_blend: bool = False       # 新版 Topaz(ffmpeg 8.1) 已移除 blend，默认关闭
    up_device: int = -2
    up_vram: float = 1.0
    up_instances: int = 1

    # NVENC 编码
    codec: str = "h264_nvenc"
    profile: str = "high"         # hevc_nvenc 在文档中配合 "main"
    qp: int = 18                  # 文档 2K 2x 那条为 17
    preset: str = "p7"
    gop: int = 30
    pix_fmt: str = "yuv420p"
    hevc_compat_flags: bool = True  # hevc 时追加 -b_ref_mode disabled -tag:v hvc1

    # 音频（ffmpeg 通用功能，与 Topaz 无关；默认与文档一致：直接 copy 流）
    copy_audio: bool = True
    audio_codec: str = "copy"       # copy / aac / libmp3lame；none = 不写 -c:a
    audio_bitrate: str = ""         # 非空时追加 -b:a（如 192k）；copy 时忽略
    # 输入是否含音轨。IMAGE 帧序列封装的临时视频**没有**音轨，此时必须跳过音频
    # 映射/元数据/bsf，否则 ffmpeg 报 "Stream specifier a:0 does not match any streams"
    # 并拒绝打开输出。默认 True（与文档原行为一致）；由节点层用 ffprobe 探测后改写。
    has_audio: bool = True

    # 其它
    overwrite: bool = False       # -y；文档原命令没有，默认保持 1:1
    videoai_metadata: str = ""    # 非空则完全覆盖自动生成的 -metadata videoai=
    mark_resolution_change: bool = False  # 追加 ". Changed resolution to WxH"


# ---------------------------------------------------------------------------
# 滤镜拼装
# ---------------------------------------------------------------------------

def build_fi_filter(cfg: TopazConfig) -> str:
    """tvai_fi=model=chf-3:slowmo=1:rdt=0.02:fps=30:device=-2:vram=1:instances=1"""
    parts = [
        f"model={cfg.fi_model}",
        f"slowmo={_num(cfg.fi_slowmo)}",
        f"rdt={_num(cfg.fi_rdt)}",
    ]
    if cfg.fi_fps and cfg.fi_fps > 0:
        parts.append(f"fps={int(cfg.fi_fps)}")
    parts += [
        f"device={cfg.fi_device}",
        f"vram={_num(cfg.fi_vram)}",
        f"instances={cfg.fi_instances}",
    ]
    return "tvai_fi=" + ":".join(parts)


def build_up_filter(cfg: TopazConfig) -> str:
    """tvai_up=model=rhea-1:scale=0:w=2560:h=1440:...:estimate=8:device=-2:..."""
    parts = [
        f"model={cfg.up_model}",
        f"scale={_num(cfg.up_scale)}",
        f"w={cfg.up_width}",
        f"h={cfg.up_height}",
        f"preblur={_num(cfg.up_preblur)}",
        f"noise={_num(cfg.up_noise)}",
        f"details={_num(cfg.up_details)}",
        f"halo={_num(cfg.up_halo)}",
        f"blur={_num(cfg.up_blur)}",
        f"compression={_num(cfg.up_compression)}",
        f"estimate={cfg.up_estimate}",
    ]
    if cfg.up_add_noise:
        parts.append(f"prenoise={_num(cfg.up_add_noise / 100)}")
    if cfg.use_blend:
        parts.append(f"blend={_num(cfg.up_blend)}")
    parts += [
        f"device={cfg.up_device}",
        f"vram={_num(cfg.up_vram)}",
        f"instances={cfg.up_instances}",
    ]
    return "tvai_up=" + ":".join(parts)


def build_filter_complex(cfg: TopazConfig) -> str:
    filters = []
    if cfg.enable_interpolation:
        filters.append(build_fi_filter(cfg))
    if cfg.enable_enhancement:
        filters.append(build_up_filter(cfg))
    if not filters:
        raise ValueError("At least one of interpolation / enhancement must be enabled.")
    return ",".join(filters)


# ---------------------------------------------------------------------------
# 元数据 / 输出路径
# ---------------------------------------------------------------------------

def build_metadata(cfg: TopazConfig) -> str:
    """复现文档里 4 种 -metadata videoai= 文案，用户可整体覆盖。"""
    override = cfg.videoai_metadata.strip()
    if override:
        return override

    chunks: list[str] = []
    if cfg.enable_interpolation:
        if cfg.fi_fps and cfg.fi_fps > 0:
            chunks.append(
                f"Framerate changed to {cfg.fi_fps} using {cfg.fi_model} "
                "replacing duplicate frames"
            )
        else:
            chunks.append(f"Processed using {cfg.fi_model} replacing duplicate frames")
    if cfg.enable_enhancement:
        text = ENHANCE_META.format(model=cfg.up_model)
        if cfg.up_add_noise:
            text = text.replace("; and recover original detail", f"; add noise at {cfg.up_add_noise}; and recover original detail")
        if cfg.mark_resolution_change:
            text += f". Changed resolution to {cfg.up_width}x{cfg.up_height}"
        chunks.append(text)
    return ". ".join(chunks)


def resolve_output_path(cfg: TopazConfig) -> str:
    """文档格式：<输入文件名去掉扩展名>_<9位数字>.mp4，同目录。

    input_path 为空（既没连 frames、也没填视频路径）时不能调 ``with_name``：
    ``Path("")`` 是 ``Path(".")``，``with_name`` 会抛
    ``ValueError: WindowsPath('.') has an empty name``。这个异常发生在
    ``validate_config`` 之前，用户看到的会是一句莫名的 pathlib 报错，而不是
    「input_path is empty」；dry_run 想只看命令时也会直接崩。
    所以这里回退到 cwd 下的占位文件名，让 dry_run 能出命令、正常执行则由
    validate_config 给出友好提示。
    """
    supplied = _normalize_path(cfg.output_path)
    if supplied:
        return supplied
    src = Path(_normalize_path(cfg.input_path))
    suffix = src.suffix or ".mp4"
    digits = random.randint(100000000, 999999999)
    if not src.name:
        return str(Path.cwd() / f"gx_topaz_output_{digits}{suffix}")
    return str(src.with_name(f"{src.stem}_{digits}{suffix}"))


def absolutize_path(value: str, *, allow_command: bool = False) -> str:
    """把路径按「当前进程 cwd」解析成绝对路径。

    ffmpeg 子进程的 cwd 会切到含 models/ 的 Topaz 数据目录（见
    _detect_topaz_home）。若不先把 -i / 输出路径 / ffmpeg_path 绝对化，
    相对路径就会在**新 cwd** 下被解析：输入找不到、输出被写进
    ProgramData，而且节点里 Path(out).parent.mkdir() 用的还是主进程 cwd，
    两边目录会对不上。

    allow_command=True 时纯命令名（"ffmpeg"，靠 PATH 查找）原样返回。
    """
    v = _normalize_path(value)
    if not v:
        return ""
    if allow_command and v == "ffmpeg":
        return v
    return os.path.abspath(v)


# ---------------------------------------------------------------------------
# 完整 argv
# ---------------------------------------------------------------------------

def build_ffmpeg_argv(cfg: TopazConfig, output_path: str | None = None) -> list[str]:
    """按文档顺序拼出 argv（list 形式，直接交给 subprocess，无需 shell 转义）。"""
    out = _normalize_path(output_path or resolve_output_path(cfg))
    input_path = _normalize_path(cfg.input_path)

    argv = [_normalize_path(cfg.ffmpeg_path), "-hide_banner"]
    if cfg.overwrite:
        argv.append("-y")
    argv += ["-i", input_path, "-sws_flags", SWS_FLAGS]
    argv += ["-filter_complex", build_filter_complex(cfg)]

    argv += ["-c:v", cfg.codec, "-profile:v", cfg.profile, "-pix_fmt", cfg.pix_fmt]
    if cfg.codec == "hevc_nvenc" and cfg.hevc_compat_flags:
        argv += ["-b_ref_mode", "disabled", "-tag:v", "hvc1"]
    argv += [
        "-g", str(cfg.gop),
        "-preset", cfg.preset,
        "-tune", "hq",
        "-rc", "constqp",
        "-qp", str(cfg.qp),
        "-rc-lookahead", "20",
        "-spatial_aq", "1",
        "-aq-strength", "15",
        "-b:v", "0",
    ]

    # 音频块：仅当 copy_audio 开启「且」输入确实含音轨时才写。
    # IMAGE 帧序列封装的临时视频无音轨，若仍写 -map_metadata:s:a:0 / -bsf:a:0 会引用
    # 不存在的输出音轨，导致 "Stream specifier a:0 does not match any streams" ->
    # "Error opening output file ... Invalid argument" 而整体失败。
    if cfg.copy_audio and cfg.has_audio:
        argv += ["-map", "0:a?", "-map_metadata:s:a:0", "0:s:a:0"]
        if cfg.audio_codec and cfg.audio_codec != "none":
            argv += ["-c:a", cfg.audio_codec]
            if cfg.audio_codec != "copy" and cfg.audio_bitrate.strip():
                argv += ["-b:a", cfg.audio_bitrate.strip()]
        if cfg.audio_codec == "copy":
            # 文档原命令在 copy 音频流时附加此 bsf
            argv += ["-bsf:a:0", "aac_adtstoasc"]
    argv += [
        "-map_metadata", "0",
        "-map_metadata:s:v", "0:s:v",
        "-fps_mode:v", "passthrough",
        "-movflags", MOV_FLAGS,
        "-bf", "0",
        "-metadata", f"videoai={build_metadata(cfg)}",
    ]
    argv.append(out)
    return argv


async def _stop_subprocess(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with suppress(ProcessLookupError):
            proc.kill()
        await proc.communicate()


async def _probe_has_audio(input_path: str, ffmpeg_path: str) -> bool:
    """用同目录的 ffprobe 探测输入是否含音轨。

    返回 True 表示有音轨（沿用文档原音频块，拷贝音频）；False 表示无音轨
    （跳过音频映射，避免 ffmpeg 因引用不存在的输出音轨而拒绝打开输出）。
    ffprobe 探测不到 / 探测失败一律保守返回 True（不破坏真实视频的音频拷贝行为）。
    """
    ff = Path(_normalize_path(ffmpeg_path))
    if not ff.is_absolute():
        return True
    probe = ff.with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if not probe.is_file():
        return True
    try:
        proc = await asyncio.create_subprocess_exec(
            probe, "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=index", "-of", "csv=p=0",
            _normalize_path(input_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_EXEC_KW,
        )
        try:
            out, _ = await proc.communicate()
        finally:
            await _stop_subprocess(proc)
    except (OSError, ValueError):
        return True
    return bool(out.decode("utf-8", "ignore").strip())


def _detect_topaz_home(ffmpeg_path: str) -> str | None:
    """找到『包含 models/ 子目录』的 Topaz 数据目录，用作 ffmpeg 子进程的 cwd。

    Topaz 的 tvai_fi / tvai_up 滤镜按『相对当前工作目录的 models/』解析模型
    （实测：cwd 指向含 models/ 的目录时模型才能加载；指向安装目录或任意其他
    目录都会报 'Model not found'）。所以必须把 ffmpeg 的 cwd 设到这个目录。

    候选（按顺序，命中第一个含 models/*.json 的即返回）：
      1. %PROGRAMDATA%\\Topaz Labs LLC\\Topaz Video AI   （6.x 模型实际落盘位置）
      2. %LOCALAPPDATA%\\Topaz Labs LLC\\Topaz Video AI
      3. ffmpeg.exe 所在目录（老版本可能把 models 放这里）
    都不命中则返回 ffmpeg 所在目录作为兜底（此时错误提示会更明确），
    没有 ffmpeg 路径时返回 None。
    """
    ff = _normalize_path(ffmpeg_path)
    candidates: list[str] = []
    for env in ("PROGRAMDATA", "LOCALAPPDATA"):
        v = os.environ.get(env)
        if v:
            candidates.append(os.path.join(v, "Topaz Labs LLC", "Topaz Video AI"))
    if ff and ff != "ffmpeg":
        candidates.append(os.path.dirname(ff))
    for c in candidates:
        models = os.path.join(c, "models")
        if os.path.isdir(models):
            try:
                if any(f.endswith(".json") for f in os.listdir(models)):
                    return c
            except OSError:
                continue
    if ff and ff != "ffmpeg":
        return os.path.dirname(ff)
    return None


def format_command(argv: list[str]) -> str:
    """按文档的展示风格输出：每个参数都用双引号包起来，可直接粘到终端执行。"""
    return " ".join('"' + str(a).replace('"', '\\"') + '"' for a in argv)


def validate_config(cfg: TopazConfig) -> str | None:
    """返回 None 表示通过，否则返回可直接显示给用户的错误说明。"""
    if not cfg.enable_interpolation and not cfg.enable_enhancement:
        return "Enable interpolation or enhancement (at least one filter is required)."
    if cfg.enable_interpolation and not re.fullmatch(r"[A-Za-z0-9_-]+", cfg.fi_model):
        return "fi_model 必须是 Topaz 模型 ID，仅支持字母、数字、连字符和下划线。"
    if cfg.enable_enhancement and not re.fullmatch(r"[A-Za-z0-9_-]+", cfg.up_model):
        return "up_model 必须是 Topaz 模型 ID，仅支持字母、数字、连字符和下划线。"
    if not _normalize_path(cfg.input_path):
        return "input_path is empty."
    if not Path(_normalize_path(cfg.input_path)).is_file():
        return f"Input file not found: {cfg.input_path}"
    ffmpeg_path = _normalize_path(cfg.ffmpeg_path)
    if ffmpeg_path != "ffmpeg" and not Path(ffmpeg_path).is_file():
        return (
            f"ffmpeg not found: {cfg.ffmpeg_path}. "
            "Point it at the ffmpeg.exe shipped with Topaz Video AI "
            "(only that build provides the tvai_fi / tvai_up filters)."
        )
    if cfg.enable_enhancement:
        if type(cfg.up_add_noise) is not int or not 0 <= cfg.up_add_noise <= 10:
            return "Add Noise must be an integer between 0 and 10."
        if cfg.up_width <= 0 or cfg.up_height <= 0:
            return "up_width / up_height must be positive."
        if cfg.up_width % 2 or cfg.up_height % 2:
            return "yuv420p requires even up_width / up_height."
    return None


# ---------------------------------------------------------------------------
# ffmpeg stderr 进度解析（纯函数，便于独立测试，不依赖 ffmpeg / ComfyUI）
# ---------------------------------------------------------------------------

def _hms_to_sec(text: str) -> float:
    """'00:05:12.34' / '05:12.34' / '12.34' -> 秒数。"""
    parts = [float(p) for p in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


def parse_progress_line(line: str, duration_sec: float | None) -> dict | None:
    """从一行 ffmpeg stderr 解析进度；不是进度 / 时长行返回 None。

    返回 dict: {kind, frame, time_sec, speed, duration_sec, text}
    kind 为 'duration' 或 'progress'，供节点层转发到界面（进度条 + 文字）。
    """
    dur = _DUR_RE.search(line)
    if dur:
        secs = _hms_to_sec(dur.group(1))
        return {
            "kind": "duration", "frame": None, "time_sec": None,
            "speed": None, "duration_sec": secs,
            "text": f"Duration={dur.group(1)}",
        }
    if "time=" in line and "frame=" in line:
        t = _TIME_RE.search(line)
        fr = _FRAME_RE.search(line)
        sp = _SPEED_RE.search(line)
        if not t or not fr:
            return None
        time_sec = _hms_to_sec(t.group(1))
        frame = int(fr.group(1))
        speed = (sp.group(1) + "x") if sp else "?"
        pct = (time_sec / duration_sec * 100) if duration_sec else None
        pct_text = f"{pct:5.1f}%" if pct is not None else "  --%"
        text = f"[{pct_text}] frame={frame} time={t.group(1)} speed={speed}"
        return {
            "kind": "progress", "frame": frame, "time_sec": time_sec,
            "speed": speed, "duration_sec": duration_sec, "text": text,
        }
    return None


# ---------------------------------------------------------------------------
# 异步执行
# ---------------------------------------------------------------------------

async def run_ffmpeg(
    argv: list[str],
    log_lines: int = 40,
    on_progress=None,
    cwd: str | None = None,
) -> tuple[int, str]:
    """异步执行 ffmpeg，不阻塞 ComfyUI 的事件循环。

    返回 (returncode, 末尾日志)。ffmpeg 的进度行用 \\r 刷新，按 \\r / \\n
    双分隔符切分；每解析到一条 Duration / 进度行，就调用 on_progress(info)
    （同步或异步回调皆可），由调用方转发到界面。

    on_progress(info) —— info 字段见 parse_progress_line。传 None 则只收集
    末尾日志（无 UI 上报，便于脱离 ComfyUI 独立验证）。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            **_EXEC_KW,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Cannot launch ffmpeg: {argv[0]}. "
            "Use the ffmpeg.exe inside the Topaz Video AI install directory."
        ) from exc

    tail: deque[str] = deque(maxlen=log_lines)
    pending = b""
    duration_sec: float | None = None
    assert proc.stderr is not None

    try:
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            pending += chunk
            # 切出完整行（\r / \n 均视为行分隔），边切边解析进度
            while True:
                lf = pending.find(b"\n")
                cr = pending.find(b"\r")
                idx_candidates = [i for i in (lf, cr) if i >= 0]
                if not idx_candidates:
                    break
                idx = min(idx_candidates)
                line = pending[:idx].decode("utf-8", errors="replace").strip()
                pending = pending[idx + 1:]
                if not line:
                    continue
                tail.append(line)
                if on_progress is not None:
                    info = parse_progress_line(line, duration_sec)
                    if info is not None:
                        if info["kind"] == "duration":
                            duration_sec = info["duration_sec"]
                        res = on_progress(info)
                        if inspect.isawaitable(res):
                            await res

        if pending.strip():
            tail.append(pending.decode("utf-8", errors="replace").strip())

        code = await proc.wait()
    finally:
        await _stop_subprocess(proc)

    # 收尾：已知总时长时把进度条推到 100%
    if on_progress is not None and duration_sec:
        res = on_progress({
            "kind": "progress", "frame": None, "time_sec": duration_sec,
            "speed": None, "duration_sec": duration_sec, "text": "[100.0%] done",
        })
        if inspect.isawaitable(res):
            await res

    return code, "\n".join(tail)
