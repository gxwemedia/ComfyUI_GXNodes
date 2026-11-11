"""ComfyUI V3 nodes for Topaz Video AI 6.2.1.

The FFmpeg command follows exports from Topaz Video AI 6.2.1.
Keep legacy widget values and option keys compatible with saved workflows.
"""

from __future__ import annotations

import os
from dataclasses import fields
from pathlib import Path

from comfy_api.latest import io

from .gx_topaz_ffmpeg import (
    FI_MODELS,
    UP_MODELS,
    TopazConfig,
    _detect_topaz_home,
    _probe_has_audio,
    absolutize_path,
    build_ffmpeg_argv,
    format_command,
    resolve_output_path,
    run_ffmpeg,
    validate_config,
)

CATEGORY = "GXNodes/Topaz Video AI"

# 只有 Topaz 自带的 ffmpeg 编译进了 tvai_fi / tvai_up 滤镜，优先自动探测。
_TOPAZ_FFMPEG_CANDIDATES = (
    r"C:\Program Files\Topaz Labs LLC\Topaz Video\ffmpeg.exe",      # 2025 改名后
    r"C:\Program Files\Topaz Labs LLC\Topaz Video AI\ffmpeg.exe",  # 旧版 / Pro
)

_CONFIG_FIELDS = {f.name for f in fields(TopazConfig)}


def _detect_ffmpeg() -> str:
    from ..gxsettings import cached_settings

    configured = cached_settings().get("topaz_ffmpeg_path")
    if configured:
        return configured
    override = os.environ.get("TOPAZ_FFMPEG", "").strip()
    if override:
        return override
    for candidate in _TOPAZ_FFMPEG_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return "ffmpeg"  # 无宿主配置时保留命令预览的占位值


# ---------------------------------------------------------------------------
# 选项命名注册表：UI 显示「人话」，背后映射回 ffmpeg / Topaz 的实际取值
#
# V3 的 Combo 只接受 list[str]，DynamicCombo.Option 也只有纯字符串 key，
# 原生不支持 (label, value) 分离。所以统一在这里做「显示名 -> 实际值」映射：
#   OPTION_CHOICES[field] 下拉里真正展示的项（有序；键 = 显示名，值 = 实际值）
#   OPTION_ALIASES[field] 旧工作流保存过的旧显示名，一并兼容
# 表里没有的原始值（h264_nvenc / p7 / yuv420p / chf-3 ...）一律原样透传，
# 因此老 workflow、手填覆盖、以及直接传技术值的调用方都不会坏。
#
# 命名格式统一为「中文用途说明 (实际技术值)」：既要让不看文档的人知道选了什么，
# 又保留技术值方便与 Topaz Video AI 导出的命令逐项核对。
# ---------------------------------------------------------------------------

OPTION_CHOICES: dict[str, dict[str, str]] = {
    "fi_model": {
        "Chronos Fast · 快速补帧 (chf-3)": "chf-3",
        "Chronos · 质量优先补帧 (chr-2)": "chr-2",
        "Apollo": "apo-8",
    },
    "up_model": {
        "Rhea · 通用放大/画质增强 (rhea-1)": "rhea-1",
        "Proteus · 细节修复/去压缩 (prob-4)": "prob-4",
    },
    "codec": {
        "H.264 / MP4 · 兼容性最好 (h264_nvenc)": "h264_nvenc",
        "H.265 / HEVC · 同画质体积更小 (hevc_nvenc)": "hevc_nvenc",
    },
    "profile": {
        "High · H.264 高规格 8-bit (high)": "high",
        "Main · H.264 通用 8-bit (main)": "main",
        "Main · HEVC 8-bit 标准动态范围 (main)": "main",
        "Main 10 · HEVC 10-bit 高动态范围 (main10)": "main10",
    },
    "preset": {
        "最慢 / 画质最好 · 文档默认 (p7)": "p7",
        "很慢 / 更高画质 (p6)": "p6",
        "慢 / 高画质 (p5)": "p5",
        "中等 / 平衡 (p4)": "p4",
        "较快 (p3)": "p3",
        "快 (p2)": "p2",
        "最快 / 画质最低 (p1)": "p1",
    },
    "pix_fmt": {
        "8-bit 4:2:0 · 通用兼容 (yuv420p)": "yuv420p",
        "10-bit 4:2:0 · HDR 流水线 (yuv420p10le)": "yuv420p10le",
        "8-bit 4:4:4 · 无损中间格式 (yuv444p)": "yuv444p",
    },
    "audio_codec": {
        "直接复制原音轨 · 不重编码 (copy)": "copy",
        "AAC 重编码 (aac)": "aac",
        "MP3 重编码 (libmp3lame)": "libmp3lame",
        "不写音频编码参数 (none)": "none",
    },
}

# 旧版 UI 用过的显示名（旧 workflow 的 widgets_values 里存的就是这些）
OPTION_ALIASES: dict[str, dict[str, str]] = {
    "fi_model": {
        "Chronos Fast (chf-3)": "chf-3",
        "Chronos (chr-2)": "chr-2",
    },
    "up_model": {
        "Rhea (rhea-1)": "rhea-1",
        "Proteus (prob-4)": "prob-4",
    },
}

OPTION_TABLES: dict[str, dict[str, str]] = {
    field: {**choices, **OPTION_ALIASES.get(field, {})}
    for field, choices in OPTION_CHOICES.items()
}


def _choices(field: str) -> list[str]:
    """某个下拉框要展示给用户的选项（不含兼容别名）。"""
    return list(OPTION_CHOICES[field].keys())


def _default_of(field: str) -> str:
    return _choices(field)[0]


def _resolve_option(selected, field: str) -> str:
    """把下拉选中的显示名解析回实际值；原始技术值 / 未知值原样透传。"""
    if selected is None:
        return ""
    key = str(selected)
    return OPTION_TABLES[field].get(key, key)


# 兼容早期版本的模块级名字（若有外部脚本 import 过）
FI_MODEL_DISPLAY = OPTION_TABLES["fi_model"]
UP_MODEL_DISPLAY = OPTION_TABLES["up_model"]


def _resolve_model(selected: str, table: dict[str, str]) -> str:
    """把下拉选中的「代表名」解析回实际的模型 id；拿到的若是原始 id 也直接返回。"""
    return table.get(selected, selected)



# ---------------------------------------------------------------------------
# DynamicCombo 取值辅助（V3 里下拉项对应的输入在 execute 中以 dict 形式到达）
# ---------------------------------------------------------------------------

def _dc_selected(kwargs: dict, cid: str) -> str:
    """取出 DynamicCombo 当前选中的选项 key。"""
    v = kwargs.get(cid)
    if isinstance(v, dict):
        return str(v.get(cid, "") or "")
    return str(v) if v is not None else ""


def _dc_extra(kwargs: dict, cid: str, extra: str):
    """取出 DynamicCombo 选项内嵌套的额外输入值（未显现时为 None）。"""
    v = kwargs.get(cid)
    if isinstance(v, dict):
        return v.get(extra)
    return None


# ---------------------------------------------------------------------------
# Schema 输入分组（三个节点复用）
# ---------------------------------------------------------------------------

def _common_inputs() -> list:
    return [
        io.Image.Input(
            "frames", display_name="images", optional=True,
        ),
        io.String.Input(
            "input_path", display_name="Input video path", default="",
        ),
        io.String.Input(
            "ffmpeg_path", display_name="FFmpeg path", default="",
        ),
        io.String.Input(
            "output_path", display_name="Output video path", default="",
        ),
        io.Float.Input(
            "fps", display_name="Source frame rate", default=30.0, min=1.0, max=240.0, step=1.0,
        ),
        io.Boolean.Input(
            "dry_run", display_name="Preview command", default=False,
        ),
        io.Int.Input(
            "max_frames", display_name="Output frame limit", default=0, min=0, max=8192, advanced=True,
        ),
    ]


def _fi_inputs() -> list:
    return [
        io.Combo.Input(
            "fi_model", display_name="Interpolation model",
            options=_choices("fi_model"),
            default=_default_of("fi_model"),
        ),
        io.Float.Input(
            "fi_slowmo", display_name="Slow motion", default=1.0,
            min=0.1, max=16.0, step=0.1, round=0.1, advanced=True,
        ),
        io.Float.Input(
            "fi_rdt", display_name="Duplicate frame sensitivity", default=0.02,
            min=0.0, max=0.2, step=0.01, round=0.001,
        ),
        io.Int.Input(
            "fi_fps", display_name="Output frame rate", default=30, min=0, max=240, step=1,
        ),
        io.String.Input(
            "fi_model_override", display_name="Fi model override", default="", advanced=True,
        ),
        io.Int.Input("fi_device", display_name="Fi device", default=-2,
                     min=-2, max=16, advanced=True, tooltip="Topaz · -2 = 自动选择 GPU。"),
        io.Float.Input("fi_vram", display_name="Fi vram", default=1.0,
                       min=0.1, max=1.0, step=0.1, advanced=True,
                       tooltip="Topaz · 允许滤镜使用的显存比例，1.0 = 全部；滤镜下限为 0.1。"),
        io.Int.Input("fi_instances", display_name="Fi instances", default=1,
                     min=0, max=3, advanced=True,
                     tooltip="Topaz · 额外模型实例数，tvai_fi 允许 0~3。"),
    ]


def _up_inputs() -> list:
    strength = dict(min=-1.0, max=1.0, step=0.01, round=0.01)
    up_choices = _choices("up_model")
    return [
        # DynamicCombo：选 Proteus 才出现 blend —— 这就是「模型不需要的参数不显示」
        io.DynamicCombo.Input(
            "up_model", display_name="Enhancement model",
            options=[
                io.DynamicCombo.Option(up_choices[0], []),
                io.DynamicCombo.Option(
                    up_choices[1],
                    [
                        io.Float.Input(
                            "up_blend", display_name="Blend",
                            default=0.2, min=0.0, max=1.0, step=0.01,
                        ),
                    ],
                ),
            ],
        ),
        io.Int.Input("up_width", display_name="Output width", default=2560,
                     min=2, max=16384, step=2, tooltip="Topaz · 输出宽度，yuv420p 要求偶数。"),
        io.Int.Input("up_height", display_name="Output height", default=1440,
                     min=2, max=16384, step=2, tooltip="Topaz · 输出高度，yuv420p 要求偶数。"),
        io.Float.Input("up_scale", display_name="Scale", default=0.0,
                       min=0.0, max=4.0, step=0.25, advanced=True,
                       tooltip="Topaz · 0 = 由上方的宽/高指定尺寸；文档 7 条命令全为 0。"
                               "tvai_up 的 scale 上限是 4。"),
        io.Float.Input("up_preblur", display_name="Preblur", default=0.0, advanced=True, **strength),
        io.Float.Input("up_noise", display_name="Reduce noise", default=0.0, advanced=True, **strength),
        io.Float.Input("up_details", display_name="Recover detail", default=0.0, advanced=True, **strength),
        io.Float.Input("up_halo", display_name="Dehalo", default=0.0, advanced=True, **strength),
        io.Float.Input("up_blur", display_name="Deblur", default=0.0, advanced=True, **strength),
        io.Float.Input("up_compression", display_name="Revert compression", default=0.0, advanced=True, **strength),
        io.Int.Input("up_estimate", display_name="Up estimate", default=8,
                     min=0, max=100, advanced=True,
                     tooltip="Topaz · 自动参数估计用多少帧，文档取值为 8；tvai_up 允许 0~100。"),
        io.Boolean.Input(
            "mark_resolution_change", display_name="Mark resolution change", default=False, advanced=True,
        ),
        io.String.Input("up_model_override", display_name="Up model override", default="", advanced=True,
                        tooltip="Topaz · 留空用下拉框的值；填写任意模型 id 可覆盖。"),
        io.Int.Input("up_device", display_name="Up device", default=-2,
                     min=-2, max=16, advanced=True, tooltip="Topaz · -2 = 自动选择 GPU。"),
        io.Float.Input("up_vram", display_name="Up vram", default=1.0,
                       min=0.1, max=1.0, step=0.1, advanced=True,
                       tooltip="Topaz · 允许滤镜使用的显存比例，1.0 = 全部；滤镜下限为 0.1。"),
        io.Int.Input("up_instances", display_name="Up instances", default=1,
                     min=0, max=3, advanced=True,
                     tooltip="Topaz · 额外模型实例数，tvai_up 允许 0~3。"),
    ]


def _encode_inputs() -> list:
    codec_choices = _choices("codec")
    profile_choices = _choices("profile")
    return [
        # DynamicCombo：选 H.265 才出现 HEVC 专用 profile 与兼容标志
        io.DynamicCombo.Input(
            "codec", display_name="Codec",
            options=[
                io.DynamicCombo.Option(
                    codec_choices[0],
                    [
                        io.Combo.Input(
                            "profile", display_name="Profile",
                            options=profile_choices[0:2], default=profile_choices[0],
                        ),
                    ],
                ),
                io.DynamicCombo.Option(
                    codec_choices[1],
                    [
                        io.Combo.Input(
                            "profile", display_name="Profile",
                            options=profile_choices[2:4], default=profile_choices[2],
                        ),
                        io.Boolean.Input(
                            "hevc_compat_flags", display_name="Hevc compat flags",
                            default=True,
                        ),
                    ],
                ),
            ],
        ),
        io.Int.Input("qp", display_name="Qp", default=18,
                     min=0, max=51, advanced=True,
                     tooltip="FFmpeg · constqp 模式的 QP 值：数值越小画质越好、文件越大；文档 2K 2x 那条为 17。"),
        io.Combo.Input("preset", display_name="Preset",
                       options=_choices("preset"), default=_default_of("preset"), advanced=True,
                       tooltip="FFmpeg · 编码速度与画质的取舍：p7 最慢画质最好（文档默认），p1 最快画质最低。"),
        io.Int.Input("gop", display_name="Gop", default=30,
                     min=1, max=600, advanced=True, tooltip="FFmpeg · 每隔多少帧放一个关键帧，文档为 30。"),
        io.Combo.Input("pix_fmt", display_name="Pix fmt",
                       options=_choices("pix_fmt"), default=_default_of("pix_fmt"), advanced=True,
                       tooltip="FFmpeg · 8-bit 4:2:0 (yuv420p) 兼容性最好；10-bit 用于 HDR 流水线。"
                               "括号内是实际像素格式名。"),
        io.Combo.Input("audio_codec", display_name="Audio codec",
                       options=_choices("audio_codec"), default=_default_of("audio_codec"), advanced=True,
                       tooltip="FFmpeg · 直接复制原音轨最快且无损；AAC/MP3 会重新编码（可配音频码率）；"
                               "none 表示不写 -c:a。输入是无音轨的帧序列时整段音频参数会自动跳过。"),
        io.String.Input("audio_bitrate", display_name="Audio bitrate", default="", advanced=True,
                        tooltip="FFmpeg · 重新编码时的音频码率，如 192k（选「直接复制原音轨」时忽略）。"),
        io.Boolean.Input("overwrite", display_name="Overwrite", default=False, advanced=True,
                         tooltip="FFmpeg · 追加 -y；文档原命令没有此参数，默认关闭以保持 1:1 复现。"),
        io.String.Input(
            "videoai_metadata", display_name="Videoai metadata", default="",
            multiline=True, advanced=True,
        ),
    ]


_INPUT_HELP = {'frames': 'Video frames as an IMAGE batch. Connect the images output of your video loader.',
 'fi_model': 'Choose Chronos Fast for speed, Chronos for quality, or Apollo for non-linear motion. '
             'Model availability depends on the installed Topaz Video AI version.',
 'fi_slowmo': '1 keeps the original speed. Larger values create slow motion, such as 2 for half '
              'speed. Audio is copied at its original speed.',
 'fi_rdt': 'Controls duplicate-frame detection. Higher values treat more similar frames as '
           'duplicates. Start with the default and increase gradually.',
 'fi_fps': 'Target output frame rate, such as 30 or 60 fps. 0 leaves the frame-rate option unset. '
           'Frame interpolation alone keeps the original image size.',
 'up_model': 'Rhea provides general enhancement and upscaling. Proteus provides adjustable '
             'restoration controls. Available controls depend on the model.',
 'up_width': 'Output width in pixels when Scale is 0. Use an even number. Set width and height in '
             'the source aspect ratio to avoid stretching.',
 'up_height': 'Output height in pixels when Scale is 0. Use an even number. 1280 x 720 to 2560 x '
              '1440 doubles both dimensions.',
 'up_scale': '0 uses Output width and Output height. A positive value requests a model scale '
             'factor. Support depends on the selected model.',
 'up_preblur': 'Adjusts pre-processing blur. Increase gradually to soften noise or artifacts '
               'before enhancement; too much can remove detail.',
 'up_noise': 'Reduces existing image noise. Higher values can remove more noise and fine texture. '
             'This is separate from Add Noise.',
 'up_details': 'Adjusts recovery of fine texture. Increase gradually; excessive values may '
               'introduce artificial detail.',
 'up_halo': 'Reduces bright or dark halos around edges. Excessive correction can soften edges.',
 'up_blur': 'Adjusts deblurring strength. Higher values may sharpen soft detail but can also '
            'introduce artifacts.',
 'up_compression': 'Reduces compression artifacts such as blocks and ringing. Increase gradually '
                   'for low-bitrate footage.',
 'up_blend': 'Mixes original source detail back into the enhanced result. 0 uses the enhanced '
             'result; 1 mixes in the most original detail. Available with Proteus.',
 'up_add_noise': 'Adds a small amount of noise to reduce an overly smooth appearance. 0 disables '
                 'it. Uses the Topaz Video AI Add Noise slider scale, from 0 to 10.'}


def _schema_inputs(*, fi: bool, up: bool) -> list:
    inputs = _common_inputs()
    if fi:
        inputs += _fi_inputs()
    if up:
        inputs += _up_inputs()
    inputs += _encode_inputs()
    if up:
        # Append the optional widget to preserve existing saved widget positions.
        inputs.append(io.Int.Input("up_add_noise", display_name="Add Noise", optional=True,
                                   default=0, min=0, max=10, step=1))
    for port in inputs:
        if port.id in _INPUT_HELP:
            port.tooltip = _INPUT_HELP[port.id]
        if isinstance(port, io.DynamicCombo.Input):
            for option in port.options:
                for nested in option.inputs:
                    if nested.id in _INPUT_HELP:
                        nested.tooltip = _INPUT_HELP[nested.id]
    return inputs


def _outputs() -> list:
    # id 保持不变（workflow 连线用它），display_name 只影响界面上的端口文字
    return [
        io.Image.Output("frames", display_name="images", tooltip="Processed video frames. The default decode limit is the first 512 frames."),
        io.String.Output("video_path", display_name="video path", tooltip="Absolute path to the complete processed video file on this ComfyUI host."),
    ]


# ---------------------------------------------------------------------------
# 共享执行逻辑
# ---------------------------------------------------------------------------

def _config_from(kwargs: dict, *, fi: bool, up: bool) -> TopazConfig:
    """把 ComfyUI 传来的命名参数收敛成 TopazConfig。

    DynamicCombo 的参数在 kwargs 里是 dict（选中项 key + 其嵌套输入），需单独析出；
    其余标量直接搬。
    """
    cfg = TopazConfig()
    for k in _CONFIG_FIELDS:
        # fi_model / up_model / codec / profile / up_blend 来自下拉或 DynamicCombo
        # 嵌套输入，单独析出；其余标量直接搬，其中下拉类字段先做显示名解析。
        if k in ("fi_model", "up_model", "codec", "profile", "up_blend"):
            continue
        v = kwargs.get(k)
        if v is None or isinstance(v, dict):
            # 校验阶段拿不到的链接输入 / 未保存的 widget：保留 TopazConfig 默认值
            continue
        if k in OPTION_TABLES:
            v = _resolve_option(v, k)
            # 老 workflow 里存的是裸技术值/旧显示名，前端换成新下拉后
            # 偶尔会提交空串；不兜底就会写出 -preset "" 这种坏参数。
            if not str(v).strip():
                v = OPTION_CHOICES[k][_default_of(k)]
        elif isinstance(v, str) and not v.strip() and not isinstance(getattr(cfg, k), str):
            # 数值/布尔 widget 被序列化成空串时（旧 workflow 常见），别把 ""
            # 写进命令（会变成 -qp ""），沿用 dataclass 默认值。
            continue
        setattr(cfg, k, v)
    cfg.enable_interpolation = fi
    cfg.enable_enhancement = up

    # fi_model（普通 Combo）：UI 选中的是显示名，背后映射回 chf-3 / chr-2
    cfg.fi_model = _resolve_option(
        kwargs.get("fi_model") or _default_of("fi_model"), "fi_model"
    )

    # up_model（DynamicCombo）：选中显示名，背后映射回 rhea-1 / prob-4
    up_model = _resolve_option(
        _dc_selected(kwargs, "up_model") or _default_of("up_model"), "up_model"
    )
    cfg.up_model = up_model
    blend = _dc_extra(kwargs, "up_model", "up_blend")
    try:
        cfg.up_blend = float(blend)
        cfg.use_blend = True
    except (TypeError, ValueError, OverflowError):
        # 未显现的嵌套输入 / 旧 workflow 的空 widget 会传 None、"", dict。
        # 一个可选参数不该让整个 execute 崩掉，沿用默认（不写 blend）。
        cfg.use_blend = False

    # codec（DynamicCombo）：选中编码器 + 该编码器专属的 profile / hevc 标志
    cfg.codec = _resolve_option(
        _dc_selected(kwargs, "codec") or _default_of("codec"), "codec"
    )
    profile = _dc_extra(kwargs, "codec", "profile")
    if profile:
        cfg.profile = _resolve_option(str(profile), "profile")
    hevc = _dc_extra(kwargs, "codec", "hevc_compat_flags")
    if hevc is not None:
        cfg.hevc_compat_flags = _safe_bool(hevc, True)
    # H.264 的默认值是 high；若切到 HEVC 而嵌套 profile 没提交上来（旧 workflow
    # 常见），high 并不是合法的 HEVC profile，回退成文档里的 main。
    if cfg.codec == "hevc_nvenc" and cfg.profile == "high":
        cfg.profile = "main"

    # 模型 id 覆盖（用户手填任意模型 id）。None 不能 str() 成 "None" 写进命令，
    # 否则 tvai 滤镜会报 Model not found: None。
    fi_override = _override_text(kwargs.get("fi_model_override"))
    if fi and fi_override:
        cfg.fi_model = fi_override
    up_override = _override_text(kwargs.get("up_model_override"))
    if up and up_override:
        cfg.up_model = up_override
    return cfg


def _override_text(value) -> str:
    """手填覆盖框：None / 空串 / 容器一律视为「没填」。"""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()


def _safe_bool(value, default: bool) -> bool:
    """Boolean widget 兼容旧 workflow 的字符串 / 数字写法。"""
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "none")
    return bool(value)


def _safe_int(value) -> int | None:
    """把 widget 值安全地转成 int；拿不到有效数值时返回 None。

    V3 的 validate_inputs 收到的是「当前能拿到的输入」：
      · 被连成输入端口（link）的 widget，此时上游还没执行，框架会传 None；
      · 旧 workflow / 前端把没保存过的 widget 序列化成 null 或空串；
      · 手填的字符串、dict（DynamicCombo 嵌套值）等也可能混进来。
    官方要求 validate_inputs 返回 bool | str；这里一旦抛异常，execution.py 会把整个
    上游节点标记成 exception_during_inner_validation，前端就报「无法校验 images」这类
    指向下游端口的误导信息。所以任何无法解释的值都只跳过该项检查，绝不抛出。
    """
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


# 只做「框架不会替我们做」的补充检查；每项都允许缺值。
_INT_CHECKS = (
    ("up_width", lambda n: n % 2 == 0, "up_width must be even for yuv420p output."),
    ("up_height", lambda n: n % 2 == 0, "up_height must be even for yuv420p output."),
    ("fi_fps", lambda n: n >= 0, "fi_fps must be >= 0 (0 = omit the fps option)."),
    ("qp", lambda n: 0 <= n <= 51, "qp must be between 0 and 51."),
)


def _validate(kwargs: dict) -> bool | str:
    """V3 的 validate_inputs。只做静态参数检查，不做文件 I/O。

    原因：官方说明 validate_inputs 只能拿到 workflow 里写死的常量，来自上游节点的
    frames / input_path 此时还取不到，检查文件会误报。文件存在性放到 execute 阶段再查。

    另外：本函数**保证不抛异常**。None / 空串 / 链接输入都跳过（用 TopazConfig 的
    默认值），数值范围仍由 ComfyUI 自身按 schema 的 min/max 校验。
    """
    try:
        for key, ok, message in _INT_CHECKS:
            raw = kwargs.get(key)
            value = _safe_int(raw)
            if value is None and key in kwargs:
                # 链接输入（上游还没执行）或旧 workflow 的空 widget 都会走到这里。
                # 打印一行，便于把「工作流无法校验」和具体参数对应起来。
                print(f"[GX Topaz] validate: {key}={raw!r} 不可用，执行阶段将用默认值")
            if value is not None and not ok(value):
                return message
        return True
    except Exception as exc:  # 兜底：校验函数抛异常会毁掉整个 prompt
        print(f"[GX Topaz] validate_inputs skipped ({type(exc).__name__}): {exc}")
        return True


# 进度上报只需要发一次性的“已开始”提示，避免长视频刷屏
_progress_started_logged = False


async def _report_progress(info: dict) -> None:
    """把 ffmpeg 进度转发到 ComfyUI 界面（节点进度条 + 文字徽标）。

    **只走一条进度通道**：经典 ``progress`` 事件（KSampler 同款，aki 前端
    必渲染节点进度条），外加 ``progress_text`` 文字徽标。

    为什么不再同时发 V3 ``progress_state``：ComfyUI 前端把这两条通道写进
    **同一个全局状态** ``te.value`` —— ``handleProgress`` 直接
    ``te.value = event.detail``，而 ``applyProgressState`` 会用
    ``display_node_id || node_id`` 重新组装并覆盖它。后端本身还会为每个节点
    自动发 start/finish 的 progress_state。逐帧双通道并发上报时互相抢占，
    在带 VHS_VideoCombine 输出节点的工作流里表现为：GX 节点的进度条显示的
    是 VideoCombine 的帧数进度，而不是 ffmpeg 百分比。收敛成单通道后不再
    自己触发 progress_state，避免抢槽位。

    文字统一带 ``[GX Topaz]`` 前缀，便于直接辨认信息到底来自哪个节点。
    进度上报失败绝不中断视频处理；中断类异常原样抛出。
    """
    global _progress_started_logged
    try:
        from comfy_execution.utils import get_executing_context
        from server import PromptServer

        ctx = get_executing_context()
        prompt_id = getattr(ctx, "prompt_id", None) if ctx is not None else None
        node_id = getattr(ctx, "node_id", None) if ctx is not None else None
        source = "executing_context"
        srv = PromptServer.instance
        if node_id is None:
            node_id = getattr(srv, "last_node_id", None)
            source = "last_node_id(fallback)"
        if prompt_id is None:
            prompt_id = getattr(srv, "last_prompt_id", None)
        if node_id is None:
            return

        raw_text = info.get("text", "")
        text = f"[GX Topaz] {raw_text}" if raw_text else "[GX Topaz]"

        # 1) 节点文字徽标：[GX Topaz] [ 38.4%] frame=600 time=... speed=...
        try:
            srv.send_progress_text(text, node_id)
        except Exception:
            pass

        # 2) 百分比：有总时长时按时间算，否则退化为按帧计数的脉冲
        dur = info.get("duration_sec")
        t = info.get("time_sec")
        if dur and t is not None and dur > 0:
            value = min(100, max(0, int(round(t / dur * 100))))
            total = 100
        elif info.get("kind") == "progress" and info.get("frame") is not None:
            value = int(info["frame"])
            total = max(1, int(info["frame"]))
        else:
            return  # duration 行等无进度信息的行，跳过

        if not _progress_started_logged:
            print(f"[GX Topaz] progress started -> node {node_id} "
                  f"(source={source}, prompt_id={prompt_id}) ({raw_text})")
            _progress_started_logged = True

        # 3) 唯一进度通道：经典 progress。补 display_node_id 与后端
        #    progress_state 的结构对齐，前端无论按 node 还是 display_node 定位
        #    都落到本节点，不会被别的节点抢占。
        try:
            srv.send_sync(
                "progress",
                {"value": value, "max": total, "prompt_id": prompt_id,
                 "node": node_id, "display_node_id": node_id},
                srv.client_id,
            )
        except Exception:
            pass
    except Exception as exc:
        name = type(exc).__name__
        if "interrupt" in name.lower():
            raise
        import traceback
        print(f"[GX Topaz] progress report skipped: {exc}\n{traceback.format_exc()}")


def _empty_image() -> "object":
    """dry_run 或异常路径下给 frames 输出一个占位的空张量，避免端口类型不匹配。"""
    import torch
    return torch.zeros((1, 8, 8, 3), dtype=torch.float32)


async def _execute(kwargs: dict, *, fi: bool, up: bool):
    global _progress_started_logged
    _progress_started_logged = False  # 每次运行重新打印一次“progress started”

    frames = kwargs.get("frames")  # 可选 IMAGE 张量
    input_path = str(kwargs.get("input_path", "") or "")
    fps = float(kwargs.get("fps", 30) or 30)
    max_frames = int(kwargs.get("max_frames", 0) or 0)
    dry_run = bool(kwargs.get("dry_run", False))

    cfg = _config_from(kwargs, fi=fi, up=up)

    cfg.input_path = absolutize_path(input_path)
    cfg.output_path = absolutize_path(cfg.output_path)
    if dry_run:
        cfg.ffmpeg_path = absolutize_path(_detect_ffmpeg(), allow_command=True)
        if frames is not None:
            cfg.input_path = "frames.mp4"  # 预览占位，实际执行时由节点封装帧序列
            cfg.has_audio = False
        out_path = absolutize_path(resolve_output_path(cfg))
        return io.NodeOutput(_empty_image(), out_path)

    if frames is not None and up and cfg.up_model == "rhea-1" and frames.shape[0] < 2:
        raise RuntimeError("[GX Topaz] Rhea 视频增强需要至少两帧 IMAGE 输入。")

    # 工作流保留 ffmpeg_path 端口供旧流程使用，但可执行文件由宿主决定。
    cfg.ffmpeg_path = _detect_ffmpeg().strip().strip('"')
    if not Path(cfg.ffmpeg_path).is_absolute() or not Path(cfg.ffmpeg_path).is_file():
        raise RuntimeError("[GX Topaz] Set the Topaz FFmpeg path in GXNodes settings.")

    # 决定处理源视频：连接了 frames 就先封装成临时视频；否则用 input_path
    temp_src = None
    try:
        if frames is not None:
            import tempfile

            from .gx_topaz_media import images_to_video

            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as temp_file:
                temp_src = temp_file.name
            print(f"[GX Topaz] encoding frames {tuple(frames.shape)} -> temp video")
            images_to_video(frames, temp_src, fps=fps)
            cfg.input_path = temp_src

        problem = validate_config(cfg)
        if problem:
            raise RuntimeError(f"[GX Topaz] {problem}")

        # 只有通过校验的输入和宿主信任的 ffprobe 才能触发子进程。
        cfg.has_audio = frames is None and await _probe_has_audio(cfg.input_path, cfg.ffmpeg_path)

        out_path = absolutize_path(resolve_output_path(cfg))
        argv = build_ffmpeg_argv(cfg, out_path)
        command_text = format_command(argv)

        # Topaz 的 tvai_fi / tvai_up 滤镜按『相对 ffmpeg 工作目录的 models/』解析模型，
        # 必须把子进程的 cwd 设到含 models/ 的 Topaz 数据目录（实测：否则报 'Model not found'）。
        topaz_home = _detect_topaz_home(cfg.ffmpeg_path)
        models_dir = os.path.join(topaz_home, "models") if topaz_home else None
        if not models_dir or not os.path.isdir(models_dir):
            print(
                f"[GX Topaz][WARN] 未找到 Topaz 模型目录（期望在 {topaz_home}/models）。"
                "若后续 ffmpeg 报 'Model not found'，请确认 Topaz Video AI 已安装且模型已下载，"
                "并检查宿主 TOPAZ_FFMPEG 的配置。"
            )
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        print(f"[GX Topaz] ffmpeg cwd: {topaz_home}")
        print(f"[GX Topaz] running: {argv[0]} -> {out_path}")
        code, log = await run_ffmpeg(argv, on_progress=_report_progress, cwd=topaz_home)
        if code != 0:
            extra = ""
            if "Model not found" in log:
                extra = (
                    "\n\n[诊断] 滤镜已加载，但找不到模型文件。Topaz 的 tvai_fi/tvai_up 会按"
                    "『相对 ffmpeg 工作目录的 models/』解析模型；本节点已把 cwd 设到含"
                    f" models/ 的目录（{topaz_home}）。若该目录不存在或不含模型，请确认"
                    " Topaz Video AI 已安装、模型已下载，且宿主 TOPAZ_FFMPEG 指向 Topaz 安装目录。"
                )
            elif "No such filter" in log:
                extra = (
                    "\n\n[诊断] 宿主配置的 FFmpeg 没有 tvai_fi/tvai_up 滤镜，请配置 Topaz 自带的"
                    " ffmpeg.exe（只有它提供这两个滤镜）。"
                )
            print(f"[GX Topaz] FFmpeg failed ({code}):\n{log}\nCommand:\n{command_text}")
            raise RuntimeError(f"[GX Topaz] Video processing failed. See the server log.{extra}")
        print(f"[GX Topaz] done: {out_path}")
        if log:
            print("[GX Topaz] ffmpeg tail:\n" + log)
        from .gx_topaz_media import video_to_images

        frames_out = video_to_images(out_path, max_frames=max_frames)
    finally:
        if temp_src and os.path.isfile(temp_src):
            try:
                os.remove(temp_src)
            except OSError:
                pass

    return io.NodeOutput(frames_out, out_path)


def _fingerprint() -> float:
    """视频处理会写外部文件，属于有副作用的节点：禁用输出缓存，每次都重新跑。

    float('NaN') != float('NaN')，因此 fingerprint 永远与上一次不同。
    """
    return float("NaN")


# ---------------------------------------------------------------------------
# 节点 1：帧插值（对应文档「# 插值」）
# ---------------------------------------------------------------------------

class GX_TopazFrameInterpolation(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="GX_TopazFrameInterpolation",
            display_name="GX Topaz Video AI Frame Interpolation",
            category=CATEGORY,
            description="Topaz Frame Interpolation with Topaz Video AI.",

            search_aliases=["Topaz", "补帧", "插帧", "帧插值", "frame interpolation", "tvai_fi", "Chronos"],
            inputs=_schema_inputs(fi=True, up=False),
            outputs=_outputs(),
            is_output_node=True,
            not_idempotent=True,
        )

    @classmethod
    def validate_inputs(cls, **kwargs) -> bool | str:
        return _validate(kwargs)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return _fingerprint()

    @classmethod
    async def execute(cls, **kwargs):
        return await _execute(kwargs, fi=True, up=False)


# ---------------------------------------------------------------------------
# 节点 2：放大 / 质量提升（对应文档「# 不放大的质量提升」等三节）
# ---------------------------------------------------------------------------

class GX_TopazEnhancement(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="GX_TopazEnhancement",
            display_name="GX Topaz Video AI Enhancement",
            category=CATEGORY,
            description="Topaz Enhancement with Topaz Video AI.",

            search_aliases=["Topaz", "放大", "超分", "画质增强", "upscale", "tvai_up", "Proteus", "Rhea"],
            inputs=_schema_inputs(fi=False, up=True),
            outputs=_outputs(),
            is_output_node=True,
            not_idempotent=True,
        )

    @classmethod
    def validate_inputs(cls, **kwargs) -> bool | str:
        return _validate(kwargs)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return _fingerprint()

    @classmethod
    async def execute(cls, **kwargs):
        return await _execute(kwargs, fi=False, up=True)


# ---------------------------------------------------------------------------
# 节点 3：插值 + 增强组合（对应文档「# 插值加质量提升」）
# ---------------------------------------------------------------------------

class GX_TopazVideoAI(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="GX_TopazVideoAI",
            display_name="GX Topaz Video AI Frame Interpolation + Enhancement",
            category=CATEGORY,
            description="Topaz Interpolation and Enhancement with Topaz Video AI.",

            search_aliases=["Topaz", "Video AI", "tvai_fi", "tvai_up", "补帧放大", "全功能"],
            inputs=_schema_inputs(fi=True, up=True),
            outputs=_outputs(),
            is_output_node=True,
            not_idempotent=True,
        )

    @classmethod
    def validate_inputs(cls, **kwargs) -> bool | str:
        return _validate(kwargs)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return _fingerprint()

    @classmethod
    async def execute(cls, **kwargs):
        return await _execute(kwargs, fi=True, up=True)


V3_NODE_CLASSES = [
    GX_TopazFrameInterpolation,
    GX_TopazEnhancement,
    GX_TopazVideoAI,
]
