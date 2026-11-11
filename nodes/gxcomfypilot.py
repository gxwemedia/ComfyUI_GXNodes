import io
import base64
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import numpy as np
import torch
from PIL import Image
from comfy_api.latest import io as comfy_io

try:
    from .. import gxsettings
except ImportError:
    gxsettings = None

_CATALOG_TIMEOUT = 20
_GENERATE_TIMEOUT = 300
_ASSET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _default_base_url():
    if gxsettings is not None:
        try:
            return str(gxsettings.load_settings().get("comfypilot_url") or "http://127.0.0.1:39112")
        except Exception:
            pass
    return os.environ.get("COMFYPILOT_URL", "http://127.0.0.1:39112")


def _text_model_cache_path():
    import folder_paths

    return Path(folder_paths.get_user_directory()) / "GXNodes" / "text_models.json"


def _cached_text_models():
    try:
        values = json.loads(_text_model_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and "::" in value][:200]


def _write_text_model_cache(values):
    path = _text_model_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass


def _text_model_options():
    """Schema options come from disk only; no network calls while ComfyUI builds the node list."""
    return ["auto", *_cached_text_models()]


def _interruptible_sleep(seconds):
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            import comfy.model_management as model_management

            model_management.throw_exception_if_processing_interrupted()
        except ImportError:
            pass
        time.sleep(min(0.25, remaining))


def _trusted_host(hostname):
    if hostname in ("127.0.0.1", "localhost", "::1"):
        return True
    if gxsettings is None:
        return False
    try:
        return bool(gxsettings.load_settings().get("allow_remote_comfypilot"))
    except Exception:
        return False


def _asset_id(asset):
    value = asset.get("assetId") if isinstance(asset, dict) else None
    if not isinstance(value, str) or not _ASSET_ID_PATTERN.match(value):
        raise RuntimeError("ComfyPilot 返回了无效的 Artifact 标识")
    return value



class GXComfyPilotConnection(comfy_io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return comfy_io.Schema(node_id="GXComfyPilotConnection", display_name="GX ComfyPilot Connection", category="GXNodes/ComfyPilot", inputs=[comfy_io.String.Input("base_url", default=_default_base_url())], outputs=[comfy_io.Custom("GX_COMFYPILOT").Output("comfypilot")])

    @classmethod
    def execute(cls, base_url):
        return comfy_io.NodeOutput({"base_url": _base_url({"base_url": base_url})})


class GXAgnesImage(comfy_io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return comfy_io.Schema(node_id="GXAgnesImage", display_name="GX Agnes Image", category="GXNodes/ComfyPilot", inputs=[
            comfy_io.String.Input("prompt", multiline=True, default=""),
            comfy_io.Combo.Input("size", ["1K"], default="1K"),
            comfy_io.Combo.Input("ratio", ["1:1", "16:9", "9:16", "4:3", "3:4"], default="1:1"),
            comfy_io.Custom("GX_COMFYPILOT").Input("comfypilot", optional=True),
            comfy_io.Image.Input("image", optional=True), comfy_io.Image.Input("image_2", optional=True),
            comfy_io.Image.Input("image_3", optional=True), comfy_io.Image.Input("image_4", optional=True),
        ], outputs=[comfy_io.Image.Output("IMAGE")])

    @staticmethod
    def _format_api_error(label, status, payload):
        if isinstance(payload, dict):
            user_message = payload.get("userMessage")
            suggestion = payload.get("suggestion")
            if isinstance(user_message, str) and user_message.strip():
                message = user_message.strip()
                if isinstance(suggestion, str) and suggestion.strip():
                    message += f" 建议：{suggestion.strip()}"
                return f"ComfyPilot {label}失败：{message}"
            detail = payload.get("error") or payload.get("message")
            if isinstance(detail, str) and detail.strip():
                return f"ComfyPilot {label}失败：{detail.strip()}"
        return f"ComfyPilot {label}失败：HTTP {status}"

    @staticmethod
    def _request_json(url, payload=None, method="POST", label="请求", timeout=_GENERATE_TIMEOUT):
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            try:
                parsed = json.loads(detail)
            except json.JSONDecodeError:
                parsed = None
            raise RuntimeError(GXAgnesImage._format_api_error(label, error.code, parsed)) from error
        except URLError as error:
            raise RuntimeError(f"无法连接 ComfyPilot：{error.reason}") from error

    @staticmethod
    def _read_artifact(url, timeout=_GENERATE_TIMEOUT):
        try:
            with urlopen(url, timeout=timeout) as response:
                return response.read()
        except HTTPError as error:
            raise RuntimeError(f"ComfyPilot 图像 Artifact 下载失败：HTTP {error.code}") from error
        except URLError as error:
            raise RuntimeError(f"无法下载 ComfyPilot 图像 Artifact：{error.reason}") from error

    @classmethod
    def execute(cls, prompt, size, ratio, comfypilot=None, image=None, image_2=None, image_3=None, image_4=None):
        if not prompt.strip():
            raise ValueError("prompt 不能为空")
        base_url = _base_url(comfypilot)
        if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
            raise ValueError("ComfyPilot 连接配置无效")
        images = _collect_image_data_uris(image, image_2, image_3, image_4)
        result = cls._request_json(
            f"{base_url}/api/agnes/images",
            {"prompt": prompt, "size": size, "ratio": ratio, **({"images": images} if images else {})},
        )
        asset = result.get("asset") if isinstance(result, dict) else None
        asset_url = result.get("url") if isinstance(result, dict) else None
        if not isinstance(asset, dict) or asset.get("kind") != "image" or asset.get("store") != "local":
            raise RuntimeError("ComfyPilot 返回的不是受控本地图像 Artifact")
        if not isinstance(asset_url, str) or not re.match(r"^/api/assets/[A-Za-z0-9_-]+$", asset_url):
            raise RuntimeError("ComfyPilot 返回了不安全的图像 Artifact 地址")
        image = Image.open(io.BytesIO(cls._read_artifact(f"{base_url}{asset_url}"))).convert("RGB")
        pixels = np.array(image).astype(np.float32) / 255.0
        return comfy_io.NodeOutput(torch.from_numpy(pixels).unsqueeze(0))


class GXAgnesText(comfy_io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return comfy_io.Schema(node_id="GXAgnesText", display_name="GX Agnes LLM", category="GXNodes/ComfyPilot", inputs=[
            comfy_io.String.Input("prompt", multiline=True, default=""),
            comfy_io.Custom("GX_COMFYPILOT").Input("comfypilot", optional=True),
            comfy_io.String.Input("system_prompt", multiline=True, default="", optional=True),
            comfy_io.Image.Input("image", optional=True),
        ], outputs=[comfy_io.String.Output("STRING")])

    @classmethod
    def execute(cls, prompt, comfypilot=None, system_prompt="", image=None):
        if not prompt.strip():
            raise ValueError("prompt 不能为空")
        base_url = _base_url(comfypilot)
        messages = []
        if system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt.strip()})
        if image is None:
            content = prompt.strip()
        else:
            content = [{"type": "text", "text": prompt.strip()}]
            content.extend(
                {
                    "type": "image_url",
                    "image_url": {"url": data_uri},
                }
                for data_uri in _image_data_uris(image)
            )
        messages.append({"role": "user", "content": content})
        return comfy_io.NodeOutput(_run_text_generation(base_url, "agnes", _agnes_text_model(), messages))


class GXComfyPilotText(comfy_io.ComfyNode):
    """通用文本节点：Provider/Model 只在 ComfyPilot 服务端目录中解析，节点不持有 API Key。"""

    @classmethod
    def define_schema(cls):
        options = _text_model_options()
        return comfy_io.Schema(node_id="GXComfyPilotText", display_name="GX ComfyPilot Text", category="GXNodes/ComfyPilot", inputs=[
            comfy_io.String.Input("prompt", multiline=True, default=""),
            comfy_io.Combo.Input("provider_model", options, default="auto"),
            comfy_io.Custom("GX_COMFYPILOT").Input("comfypilot", optional=True),
            comfy_io.String.Input("system_prompt", multiline=True, default="", optional=True),
            comfy_io.Image.Input("image", optional=True),
        ], outputs=[comfy_io.String.Output("STRING")])

    @classmethod
    def execute(cls, prompt, provider_model, comfypilot=None, system_prompt="", image=None):
        if not prompt.strip():
            raise ValueError("prompt 不能为空")
        base_url = _base_url(comfypilot)
        messages = []
        if system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt.strip()})
        if image is None:
            messages.append({"role": "user", "content": prompt.strip()})
        else:
            messages.append({"role": "user", "content": [{"type": "text", "text": prompt.strip()}] + [{"type": "image_url", "image_url": {"url": data_uri}} for data_uri in _image_data_uris(image)]})
        provider_id, model_id = _resolve_text_provider(base_url, provider_model)
        return comfy_io.NodeOutput(_run_text_generation(base_url, provider_id, model_id, messages))


class GXAgnesVideo(comfy_io.ComfyNode):
    POLL_INTERVAL_SECONDS = 2
    POLL_TIMEOUT_SECONDS = 15 * 60

    @classmethod
    def define_schema(cls):
        return comfy_io.Schema(node_id="GXAgnesVideo", display_name="GX Agnes Video", category="GXNodes/ComfyPilot", inputs=[
            comfy_io.String.Input("prompt", multiline=True, default=""),
            comfy_io.Combo.Input("ratio", ["16:9", "9:16", "1:1", "4:3", "3:4"], default="16:9"),
            comfy_io.Int.Input("num_frames", default=121, min=1, max=441, step=8),
            comfy_io.Int.Input("frame_rate", default=24, min=1, max=60),
            comfy_io.Custom("GX_COMFYPILOT").Input("comfypilot", optional=True),
            comfy_io.Combo.Input("mode", ["text-to-video", "image-to-video", "keyframe-video"], default="text-to-video", optional=True),
            comfy_io.Int.Input("seed", default=0, min=0, optional=True),
            comfy_io.String.Input("negative_prompt", multiline=True, default="", optional=True),
            comfy_io.Image.Input("image", optional=True), comfy_io.Image.Input("first_frame", optional=True), comfy_io.Image.Input("last_frame", optional=True),
        ], outputs=[comfy_io.Video.Output("VIDEO")])

    @classmethod
    def execute(cls, prompt, ratio, num_frames, frame_rate, comfypilot=None, mode="text-to-video", seed=0, negative_prompt="", image=None, first_frame=None, last_frame=None):
        if not prompt.strip():
            raise ValueError("prompt 不能为空")
        if (num_frames - 1) % 8 != 0:
            raise ValueError("num_frames 必须是 8n + 1")
        if mode == "image-to-video" and image is None:
            raise ValueError("image-to-video 必须连接 image 输入")
        if mode == "keyframe-video" and (first_frame is None or last_frame is None):
            raise ValueError("keyframe-video 必须同时连接 first_frame 和 last_frame 输入")
        if mode == "text-to-video" and any(value is not None for value in (image, first_frame, last_frame)):
            raise ValueError("text-to-video 不应连接图片输入")
        base_url = _base_url(comfypilot)
        task_id = f"agnes-video-{uuid.uuid4().hex}"
        params = {
            "prompt": prompt.strip(),
            "ratio": ratio,
            "numFrames": num_frames,
            "frameRate": frame_rate,
        }
        if seed is not None:
            params["seed"] = seed
        if negative_prompt.strip():
            params["negativePrompt"] = negative_prompt.strip()
        input_bindings = []
        for port, value in (("image", image), ("firstFrame", first_frame), ("lastFrame", last_frame)):
            if value is None:
                continue
            data_uri = _collect_image_data_uris(value)[0]
            asset = _import_image_asset(base_url, data_uri)
            input_bindings.append({"inputPort": port, "artifact": asset})
        capability = {
            "text-to-video": "agnes.text-to-video",
            "image-to-video": "agnes.image-to-video",
            "keyframe-video": "agnes.keyframe-video",
        }[mode]
        run = GXAgnesImage._request_json(
            f"{base_url}/api/runs",
            {
                "graphId": task_id,
                "tasks": [{
                    "id": "video",
                    "backend": "agnes",
                    "capability": capability,
                    "params": params,
                    "inputs": input_bindings,
                }],
                "edges": [],
            },
            label="视频 Run 请求",
        )
        run_id = run.get("runId") if isinstance(run, dict) else None
        if not isinstance(run_id, str) or not run_id:
            raise RuntimeError("ComfyPilot 未返回 Run ID")
        grant = GXAgnesImage._request_json(f"{base_url}/api/runs/{run_id}/approve", {"scope": "execute"}, label="视频审批请求")
        grant_id = grant.get("grantId") if isinstance(grant, dict) else None
        if not isinstance(grant_id, str) or not grant_id:
            raise RuntimeError("ComfyPilot 未返回 approval grant")
        executed = GXAgnesImage._request_json(
            f"{base_url}/api/execute",
            {"runId": run_id, "options": {"approvalGrantId": grant_id}},
            label="视频执行请求",
        )
        backend_job_id = executed.get("backendJobId") if isinstance(executed, dict) else None
        deadline = time.monotonic() + cls.POLL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            record = GXAgnesImage._request_json(f"{base_url}/api/runs/{run_id}", method="GET", label="视频状态请求", timeout=_CATALOG_TIMEOUT)
            asset = _find_video_asset(record)
            if asset:
                return comfy_io.NodeOutput(_download_video_asset(base_url, asset))
            if isinstance(record, dict) and record.get("run", {}).get("state") == "error":
                raise RuntimeError("ComfyPilot Agnes 视频 Run 执行失败")
            GXAgnesImage._request_json(
                f"{base_url}/api/runs/{run_id}/reconcile",
                {"backendJobId": backend_job_id} if backend_job_id else {},
                label="视频对账请求",
                timeout=_CATALOG_TIMEOUT,
            )
            _interruptible_sleep(cls.POLL_INTERVAL_SECONDS)
        raise TimeoutError("等待 ComfyPilot Agnes 视频 Artifact 超时")


def _base_url(comfypilot):
    base_url = comfypilot.get("base_url") if isinstance(comfypilot, dict) else _default_base_url()
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("ComfyPilot 连接配置无效")
    base_url = base_url.strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("ComfyPilot 地址必须是有效的 http(s) 地址")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("ComfyPilot 地址只能包含协议、主机和端口")
    if not _trusted_host(parsed.hostname):
        raise ValueError("非本机 ComfyPilot 地址需要先在 GXNodes 设置中启用“允许远程 ComfyPilot 地址”")
    return base_url


def _catalog(base_url):
    payload = GXAgnesImage._request_json(f"{base_url}/api/providers", method="GET", label="Provider Catalog 请求", timeout=_CATALOG_TIMEOUT)
    providers = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(providers, list):
        raise RuntimeError("ComfyPilot 未返回有效 Provider Catalog")
    try:
        values = [
            f"{provider.get('id')}::{model.get('id')}"
            for provider in providers
            if isinstance(provider, dict) and provider.get("enabled")
            for model in provider.get("models", [])
            if isinstance(model, dict) and model.get("enabled") and model.get("modality") == "text"
        ]
        _write_text_model_cache(values)
    except Exception:
        pass
    return providers


def _agnes_text_model():
    return os.environ.get("AGNES_TEXT_MODEL", "agnes-2.0-flash")


def _resolve_text_provider(base_url, provider_model):
    providers = _catalog(base_url)
    if provider_model and provider_model != "auto" and "::" in provider_model:
        provider_id, model_id = provider_model.split("::", 1)
        for provider in providers:
            if provider.get("id") != provider_id or not provider.get("enabled"):
                continue
            if any(model.get("id") == model_id and model.get("enabled") and model.get("modality") == "text" for model in provider.get("models", [])):
                return provider_id, model_id
    for provider in providers:
        if provider.get("enabled"):
            for model in provider.get("models", []):
                if model.get("enabled") and model.get("modality") == "text":
                    return provider.get("id"), model.get("id")
    raise RuntimeError("ComfyPilot 当前没有可用的文本 Provider/Model")


def _run_text_generation(base_url, provider_id, model_id, messages):
    selected = None
    for provider in _catalog(base_url):
        if provider.get("id") == provider_id and provider.get("enabled"):
            selected = next((model for model in provider.get("models", []) if model.get("id") == model_id and model.get("enabled") and model.get("modality") == "text"), None)
            break
    if not selected:
        raise RuntimeError("所选 ComfyPilot Provider/Model 当前不可用")
    task_id = f"comfypilot-text-{uuid.uuid4().hex}"
    run = GXAgnesImage._request_json(f"{base_url}/api/runs", {"graphId": task_id, "tasks": [{"id": "text", "backend": selected["backend"], "capability": selected["capability"], "params": {"messages": messages}}], "edges": []}, label="文本 Run 请求")
    run_id = run.get("runId") if isinstance(run, dict) else None
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeError("ComfyPilot 未返回 Run ID")
    grant = GXAgnesImage._request_json(f"{base_url}/api/runs/{run_id}/approve", {"scope": "execute"}, label="文本审批请求")
    grant_id = grant.get("grantId") if isinstance(grant, dict) else None
    if not isinstance(grant_id, str) or not grant_id:
        raise RuntimeError("ComfyPilot 未返回 approval grant")
    GXAgnesImage._request_json(f"{base_url}/api/execute", {"runId": run_id, "options": {"approvalGrantId": grant_id}}, label="文本执行请求")
    deadline = time.monotonic() + _GENERATE_TIMEOUT
    while time.monotonic() < deadline:
        record = GXAgnesImage._request_json(f"{base_url}/api/runs/{run_id}", method="GET", label="文本 Run 状态请求", timeout=_CATALOG_TIMEOUT)
        asset = next((item for item in (record.get("assets") if isinstance(record, dict) else []) or [] if isinstance(item, dict) and item.get("kind") == "text"), None)
        if asset:
            return GXAgnesImage._read_artifact(f"{base_url}/api/assets/{_asset_id(asset)}").decode("utf-8")
        if isinstance(record, dict) and record.get("run", {}).get("state") == "error":
            raise RuntimeError("ComfyPilot 文本 Run 执行失败")
        _interruptible_sleep(2)
    raise TimeoutError("等待 ComfyPilot 文本 Artifact 超时")


def _collect_image_data_uris(*images):
    data_uris = []
    for image in images:
        if image is not None:
            data_uris.extend(_image_data_uris(image))
    return data_uris


def _import_image_asset(base_url, data_uri):
    header, encoded = data_uri.split(",", 1)
    media_type = header[5:].split(";", 1)[0] or "image/png"
    request = Request(
        f"{base_url}/api/assets/import-image",
        data=base64.b64decode(encoded),
        headers={"Content-Type": media_type},
        method="POST",
    )
    try:
        with urlopen(request, timeout=300) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"ComfyPilot 图片 Artifact 导入失败：HTTP {error.code} {detail}") from error
    except URLError as error:
        raise RuntimeError(f"无法连接 ComfyPilot 导入图片 Artifact：{error.reason}") from error
    asset = payload.get("asset") if isinstance(payload, dict) else None
    if not isinstance(asset, dict) or asset.get("store") != "local" or asset.get("kind") != "image":
        raise RuntimeError("ComfyPilot 未返回受控图片 Artifact")
    return asset


def _image_data_uris(image):
    frames = image.detach().cpu().numpy() if hasattr(image, "detach") else np.asarray(image)
    if frames.ndim == 3:
        frames = frames[None, ...]
    if frames.ndim != 4 or frames.shape[-1] not in (3, 4):
        raise ValueError("LLM 图片输入必须是 ComfyUI IMAGE 类型")
    data_uris = []
    for frame in frames:
        pixels = np.clip(frame[..., :3] * 255.0, 0, 255).astype(np.uint8)
        buffer = io.BytesIO()
        Image.fromarray(pixels).save(buffer, format="PNG", optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        data_uris.append(f"data:image/png;base64,{encoded}")
    return data_uris


def _find_video_asset(record):
    assets = record.get("assets") if isinstance(record, dict) else None
    for asset in assets or []:
        if isinstance(asset, dict) and asset.get("kind") == "video" and asset.get("store") == "local":
            if isinstance(asset.get("assetId"), str) and _ASSET_ID_PATTERN.match(asset["assetId"]):
                return asset
    return None


def _download_video_asset(base_url, asset):
    asset_id = _asset_id(asset)
    data = GXAgnesImage._read_artifact(f"{base_url}/api/assets/{asset_id}")
    import folder_paths
    from comfy_api.latest import InputImpl

    temp_dir = folder_paths.get_temp_directory()
    os.makedirs(temp_dir, exist_ok=True)
    extension = ".webm" if asset.get("mediaType") == "video/webm" else ".mp4"
    path = os.path.join(temp_dir, f"gx-agnes-{asset_id}{extension}")
    with open(path, "wb") as output:
        output.write(data)
    return InputImpl.VideoFromFile(path)


V3_NODE_CLASSES = [GXComfyPilotConnection, GXAgnesImage, GXAgnesText, GXComfyPilotText, GXAgnesVideo]
