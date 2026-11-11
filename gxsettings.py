"""Persistent feature settings shared by the GXNodes backend and frontend."""
import json
import errno
import os
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import folder_paths
from aiohttp import web

try:
    from . import gxsecurity
except ImportError:
    import gxsecurity

DEFAULTS = {
    "enable_comfypilot": True,
    "enable_shared_compute": True,
    "allow_incoming_shared": True,
    "comfypilot_url": "http://127.0.0.1:39112",
    "allow_remote_comfypilot": False,
    "shared_token": "",
    "topaz_ffmpeg_path": "",
    "result_retention_days": 0,
    "transfer_retention_hours": 24,
}

_BOOL_KEYS = {
    "enable_comfypilot",
    "enable_shared_compute",
    "allow_incoming_shared",
    "allow_remote_comfypilot",
}
_INT_KEYS = {"result_retention_days", "transfer_retention_hours"}

# Node ids owned by a feature switch. Disabling the feature removes them from the
# live node registry so the node menu updates without restarting ComfyUI.
NODE_FEATURES = {
    "enable_comfypilot": (
        "GXComfyPilotConnection",
        "GXAgnesImage",
        "GXAgnesText",
        "GXComfyPilotText",
        "GXAgnesVideo",
    ),
    "enable_shared_compute": (
        "GXSharedLoadImage",
        "GXSharedLoadAudio",
        "GXSharedLoadVideo",
    ),
}

_lock = threading.Lock()
_started = False
_cache = {"at": 0.0, "value": None}
_settings_file_seen = False
_node_classes = {}
_node_display_names = {}


class SettingsBusyError(OSError):
    """A competing cooperating process owns the settings transaction."""


def _settings_path():
    return Path(folder_paths.get_user_directory()) / "GXNodes" / "settings.json"


def _settings_lock_path():
    path = _settings_path()
    return path.with_name(path.name + ".lock")


@contextmanager
def _exclusive_settings_lock():
    """One nonblocking attempt at the cooperative inter-process lock."""
    path = _settings_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("设置锁文件不是普通文件")
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN} or getattr(error, "winerror", None) == 33:
                raise SettingsBusyError("设置正由其他实例保存，请稍后重试") from error
            raise
        try:
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                print("[ComfyUI_GXNodes] 设置保存锁释放失败；请检查用户目录。")
    finally:
        os.close(fd)


def _legacy_settings_path():
    return Path(folder_paths.get_user_directory()) / "GXShared" / "settings.json"


def _read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_saved_settings():
    global _settings_file_seen
    try:
        raw = _settings_path().read_text(encoding="utf-8")
    except FileNotFoundError as error:
        if _settings_file_seen:
            raise OSError("GXNodes 设置文件已丢失") from error
        return {}
    _settings_file_seen = True
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("GXNodes 设置文件无效")
    return value


def incoming_shared_allowed():
    # Read the authoritative file at admission, not a potentially stale cache.
    with _lock:
        saved = _read_saved_settings()
        value = saved.get("allow_incoming_shared", True)
        return value if isinstance(value, bool) else False


def _is_loopback_url(value):
    parsed = urlparse(value)
    return parsed.hostname in ("127.0.0.1", "localhost", "::1")



def _validate(key, value, settings):
    if key in _BOOL_KEYS:
        if not isinstance(value, bool):
            raise ValueError(f"{key} 必须是布尔值")
        return value
    if key in _INT_KEYS:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{key} 必须是非负整数")
        return value
    if key == "comfypilot_url":
        if not isinstance(value, str) or not value.strip():
            raise ValueError("comfypilot_url 不能为空")
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("comfypilot_url 必须是 http(s) 地址")
        if not settings.get("allow_remote_comfypilot") and not _is_loopback_url(normalized):
            raise ValueError("非本机 ComfyPilot 地址需要先启用“允许远程 ComfyPilot 地址”")
        return normalized
    if key == "topaz_ffmpeg_path":
        if not isinstance(value, str):
            raise ValueError("Topaz FFmpeg path must be text")
        value = value.strip().strip('"')
        if value:
            path = Path(value)
            if not path.is_absolute() or path.drive.startswith(("\\\\", "//")) or path.name.lower() != "ffmpeg.exe":
                raise ValueError("Enter the absolute local path to Topaz ffmpeg.exe")
        return value
    if key == "shared_token":
        if not isinstance(value, str) or len(value) > 256:
            raise ValueError("shared_token 必须是长度不超过 256 的字符串")
        return value.strip()
    raise ValueError(f"未知设置项：{key}")


def _apply(settings, values):
    # Booleans first so "allow remote + remote url" patches validate in one shot.
    for key in sorted(values, key=lambda name: (name not in _BOOL_KEYS, name)):
        if key not in DEFAULTS:
            continue
        try:
            settings[key] = _validate(key, values[key], settings)
        except ValueError:
            continue
    return settings


def _load_uncached(saved=None):
    settings = dict(DEFAULTS)
    legacy = _read_json(_legacy_settings_path())
    for key in _INT_KEYS:
        value = legacy.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            settings[key] = value
    try:
        if saved is None:
            saved = _read_saved_settings()
    except (OSError, ValueError, UnicodeError):
        settings["allow_incoming_shared"] = False
        return settings
    result = _apply(settings, saved)
    if "allow_incoming_shared" in saved and not isinstance(saved["allow_incoming_shared"], bool):
        result["allow_incoming_shared"] = False
    return result


def load_settings():
    with _lock:
        settings = _load_uncached()
        _cache["value"], _cache["at"] = settings, time.monotonic()
    return dict(settings)


def cached_settings(max_age=1.5):
    """Cached read for polling loops; keeps background threads off the disk."""
    now = time.monotonic()
    with _lock:
        value = _cache["value"]
        if value is not None and now - _cache["at"] <= max_age:
            return dict(value)
    return load_settings()


def save_settings(patch):
    global _settings_file_seen
    if not isinstance(patch, dict):
        raise ValueError("设置必须是 JSON 对象")
    unknown = set(patch) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"未知设置项：{', '.join(sorted(unknown))}")
    with _lock, _exclusive_settings_lock():
        # Never replace a damaged settings file with defaults during a save.
        # Use the validated snapshot; a second read must not silently fall back to defaults.
        saved = _read_saved_settings()
        settings = _load_uncached(saved)
        for key in sorted(patch, key=lambda name: (name not in _BOOL_KEYS, name)):
            settings[key] = _validate(key, patch[key], settings)
        path = _settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(settings, ensure_ascii=False, indent=2).encode("utf-8")
        fd = None
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=str(path.parent))
            with os.fdopen(fd, "wb") as handle:
                fd = None  # The file object now owns and closes this descriptor.
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            published_at = time.monotonic()
            os.replace(temporary, path)
            temporary = None  # Published; never delete a path someone else may recreate.
        except BaseException:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    print("[ComfyUI_GXNodes] 设置临时文件清理失败；请检查用户目录。")
            raise
        _settings_file_seen = True
        _cache["value"], _cache["at"] = settings, published_at
    return settings


def _incoming_control_available():
    try:
        try:
            from . import gxshared
        except ImportError:
            import gxshared
        return bool(gxshared._incoming_hook_ready())
    except Exception:
        return False


def public_settings():
    settings = load_settings()
    result = {key: value for key, value in settings.items() if key != "shared_token"}
    result["shared_token_set"] = bool(settings["shared_token"])
    result["incoming_shared_available"] = _incoming_control_available()
    return result


def public_features():
    settings = cached_settings(0.5)
    return {
        "enable_comfypilot": settings["enable_comfypilot"],
        "enable_shared_compute": settings["enable_shared_compute"],
        "comfypilot_url": settings["comfypilot_url"],
    }


def feature_enabled(name):
    return bool(cached_settings(0.5).get(name, True))


def disabled_node_ids(settings=None):
    settings = settings or load_settings()
    disabled = set()
    for feature, node_ids in NODE_FEATURES.items():
        if not settings.get(feature, True):
            disabled.update(node_ids)
    return disabled


def register_node_classes(classes):
    """Remember every imported node so visibility can be flipped at runtime."""
    for node_class in classes:
        try:
            schema = node_class.GET_SCHEMA() if hasattr(node_class, "GET_SCHEMA") else node_class.define_schema()
            node_id = schema.node_id
        except Exception:
            continue
        _node_classes[node_id] = node_class
        _node_display_names[node_id] = getattr(schema, "display_name", None) or node_id


def node_id_of(node_class):
    try:
        schema = node_class.GET_SCHEMA() if hasattr(node_class, "GET_SCHEMA") else node_class.define_schema()
        return schema.node_id
    except Exception:
        return None


def apply_node_visibility(settings=None):
    """Add/remove GX nodes from the live registry; /object_info reads it per request."""
    settings = settings or load_settings()
    disabled = disabled_node_ids(settings)
    try:
        import nodes
    except ImportError:
        return
    mappings = getattr(nodes, "NODE_CLASS_MAPPINGS", None)
    display = getattr(nodes, "NODE_DISPLAY_NAME_MAPPINGS", None)
    if mappings is None:
        return
    for node_id, node_class in _node_classes.items():
        if node_id in disabled:
            mappings.pop(node_id, None)
            if display is not None:
                display.pop(node_id, None)
            continue
        if node_id in mappings:
            continue
        if getattr(node_class, "RELATIVE_PYTHON_MODULE", None) is None:
            node_class.RELATIVE_PYTHON_MODULE = "custom_nodes.ComfyUI_GXNodes"
        mappings[node_id] = node_class
        if display is not None:
            display[node_id] = _node_display_names.get(node_id, node_id)


def effective_token():
    # A configured environment token does not excuse an unreadable settings file.
    with _lock:
        settings = _read_saved_settings()
        saved = settings.get("shared_token", "")
        if not isinstance(saved, str) or len(saved) > 256:
            raise ValueError("共享令牌配置无效")
        return os.environ.get("GX_SHARED_TOKEN", "").strip() or saved.strip()


def retention_settings():
    settings = load_settings()
    return {
        "result_retention_days": settings["result_retention_days"],
        "transfer_retention_hours": settings["transfer_retention_hours"],
    }


def _cleanup_after_settings(saved):
    try:
        try:
            from . import gxshared
        except ImportError:
            import gxshared

        gxshared._cleanup_expired(saved)
    except Exception as error:
        print("[ComfyUI_GXNodes] settings cleanup failed: %s" % error)


async def _config_route(request):
    denied = gxsecurity.guard(request)
    if denied:
        return denied
    return web.json_response(public_features())


async def _settings_route(request):
    denied = gxsecurity.guard(request)
    if denied:
        return denied
    if request.method == "GET":
        return web.json_response({"settings": public_settings()})
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
    if "topaz_ffmpeg_path" in body and (
        request.remote not in gxsecurity._LOOPBACK
        or request.headers.get("X-GX-Token") != gxsecurity.token()
    ):
        return web.json_response({"error": "Topaz FFmpeg can only be configured locally with a GX session", "code": "gx_local_only"}, status=403)
    if "allow_incoming_shared" in body:
        if request.remote not in gxsecurity._LOOPBACK:
            return web.json_response({"error": "仅目标本机可以更改共享接单设置", "code": "gx_local_only"}, status=403)
        if not _incoming_control_available():
            return web.json_response({"error": "共享接单控制未就绪，设置未保存", "code": "gx_incoming_unavailable"}, status=503)
    try:
        saved = save_settings(body)
    except SettingsBusyError:
        return web.json_response({"error": "设置正由其他实例保存，请稍后重试", "code": "gx_settings_busy"}, status=409)
    except (OSError, ValueError) as error:
        return web.json_response({"error": str(error)}, status=400)
    apply_node_visibility(saved)
    if any(key in body for key in _INT_KEYS):
        threading.Thread(target=_cleanup_after_settings, args=(saved,), daemon=True).start()
    return web.json_response({"ok": True, "settings": public_settings()})


def register_settings_routes():
    global _started
    if _started:
        return
    _started = True
    from server import PromptServer

    PromptServer.instance.routes.get("/gx/config")(_config_route)
    PromptServer.instance.routes.get("/gx/settings")(_settings_route)
    PromptServer.instance.routes.post("/gx/settings")(_settings_route)
