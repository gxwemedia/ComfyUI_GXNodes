"""局域网共享算力：发现实例、迁移受控资源、转发任务并回传媒体结果。"""

import asyncio
import copy
import hashlib
from collections import Counter
import http.client
import inspect
import ipaddress
import json
import ntpath
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import aiohttp
from aiohttp import web
from aiohttp.payload import Payload
import folder_paths
import nodes
from server import PromptServer

try:
    from . import gxsecurity, gxsettings, gxshared_snapshot
except ImportError:
    import gxsecurity
    import gxsettings
    import gxshared_snapshot

try:
    from . import gxshared_receive
except ImportError:
    import gxshared_receive

_PORT = 8765
_PROTOCOL_VERSION = 2
_SUBMISSION_UNKNOWN = "远端是否入队未知，请先核对目标队列，勿重复提交"
_ACCEPTED_SAVE_UNCONFIRMED = "远端已接受任务，本地保存状态未确认，请勿重复提交"
_MAX_WORKFLOW_NAME = 120
_RESOURCE_CONTRACT_VERSION = 2
_MAX_RESOURCE_BYTES = 1024 * 1024 * 1024
_MAX_RESOURCE_REFS = 4096
_LARGE_RESOURCE_BYTES = 100 * 1024 * 1024
# Verified against ComfyUI 0.44 official nodes.py: CLIPTextEncode.text is a multiline STRING prompt.
# Other inputs require positive upload semantics before migration.
_TEXT_ONLY_FIELDS = {"CLIPTextEncode": frozenset({"text"})}
_UPLOAD_MARKERS = ("image_upload", "audio_upload", "video_upload", "file_upload")


def _declared_inputs(node_class):
    definitions = node_class.INPUT_TYPES()
    if not isinstance(definitions, dict):
        raise ValueError("无效节点输入定义")
    return definitions.get("required", {}) | definitions.get("optional", {})


def _is_resource_input(node_class, name, config):
    if (
        isinstance(config, (tuple, list))
        and len(config) > 1
        and isinstance(config[1], dict)
        and any(config[1].get(marker) is True for marker in _UPLOAD_MARKERS)
    ):
        return True
    # GX result readers use a STRING reference rather than an upload widget.
    module = sys.modules.get(getattr(node_class, "__module__", ""))
    return (
        name == "result"
        and module is not None
        and module.__name__.endswith(".gxsharedloadmedia")
        and getattr(node_class, "__name__", "")
        in {"GXSharedLoadImage", "GXSharedLoadAudio", "GXSharedLoadVideo"}
        and getattr(module, node_class.__name__, None) is node_class
    )


def _active_text_only_fields():
    """Only trust the exact official CLIPTextEncode class in the source registry."""
    try:
        official = getattr(nodes, "CLIPTextEncode", None)
        mapping = getattr(nodes, "NODE_CLASS_MAPPINGS", None)
        if (
            official is not None
            and isinstance(mapping, dict)
            and mapping.get("CLIPTextEncode") is official
        ):
            return _TEXT_ONLY_FIELDS
    except (AttributeError, TypeError):
        pass
    return {}


_REMOTE_TIMEOUT_SECONDS = 30
_JOB_STATUS_TIMEOUT_SECONDS = 3
_MAX_JOB_STATUS_BYTES = 1024 * 1024
_STALE_SECONDS = 8
_MONITOR_RETRIES = 5
_peers = {}
_jobs = {}
_monitors = set()
_lock = threading.Lock()
_started = False
_restored = False


def _shared_enabled():
    try:
        return bool(gxsettings.cached_settings(1.5).get("enable_shared_compute", True))
    except Exception:
        return True


def _incoming_allowed():
    try:
        return gxsettings.incoming_shared_allowed()
    except Exception:
        # ComfyUI swallows prompt handler exceptions, so failures must deny here.
        return False


def _incoming_denied():
    return web.json_response(
        {"error": "目标实例已暂停接收共享任务", "code": "gx_incoming_disabled"},
        status=409,
    )


def _guard_shared_prompt(data):
    """Apply the receiver policy at the native prompt handler admission point."""
    if not isinstance(data, dict):
        return data
    extra = data.get("extra_data")
    if (
        isinstance(extra, dict)
        and extra.get("gx_shared") is True
        and not _incoming_allowed()
    ):
        return {key: value for key, value in data.items() if key != "prompt"}
    return data


@web.middleware
async def _incoming_prompt_middleware(request, handler):
    if request.method == "POST" and request.path in {"/prompt", "/api/prompt"}:
        transport = request.transport
        peer = transport.get_extra_info("peername") if transport else None
        try:
            address = ipaddress.ip_address(peer[0]) if peer else None
            mapped = getattr(address, "ipv4_mapped", None)
            local = bool(
                address and (address.is_loopback or (mapped and mapped.is_loopback))
            )
        except (ValueError, TypeError, IndexError):
            local = False
        if not local and not _incoming_allowed():
            return _incoming_denied()
    return await handler(request)


def _incoming_hook_ready():
    try:
        instance = PromptServer.instance
        return (
            bool(_started)
            and _guard_shared_prompt in getattr(instance, "on_prompt_handlers", ())
            and _incoming_prompt_middleware in instance.app.middlewares
        )
    except (AttributeError, TypeError):
        return False


def _disabled_response():
    return web.json_response({"error": "共享算力已在 GXNodes 设置中关闭"}, status=403)


def _route_denied(request):
    denied = gxsecurity.guard(request)
    if denied:
        return denied
    if not _shared_enabled():
        return _disabled_response()
    return None


def _local_registry_dir():
    root = os.environ.get("GX_SHARED_REGISTRY_DIR")
    if root:
        return Path(root)
    return (
        Path(os.environ.get("LOCALAPPDATA", Path.home()))
        / "ComfyUI_GXNodes"
        / "shared-peers"
    )


def _jobs_dir():
    return Path(folder_paths.get_user_directory()) / "GXShared" / "jobs"


def _output_root():
    return Path(folder_paths.get_output_directory()).resolve()


def _local_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _listening_port():
    port = getattr(PromptServer.instance, "port", None)
    return (
        port
        if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536
        else None
    )


def _identity(port=None):
    port = _listening_port() if port is None else port
    if port is None:
        return None
    return f"gxnodes-{socket.gethostname()}-{_local_ip()}-{port}"


def _status_payload():
    # ComfyUI assigns instance.port only after its HTTP site has started.
    port = _listening_port()
    if port is None:
        return None
    running, pending = PromptServer.instance.prompt_queue.get_current_queue_volatile()
    return {
        "id": _identity(port),
        "name": socket.gethostname(),
        "host": _local_ip(),
        "port": port,
        "queue": len(running) + len(pending),
        "running": len(running),
        "hardware": _hardware_status(),
        "protocol": _PROTOCOL_VERSION,
        "seen": time.time(),
        "allow_incoming_shared": _incoming_allowed()
        if _incoming_hook_ready()
        else None,
    }


def _hardware_status():
    import torch

    if not torch.cuda.is_available():
        return {"device": "CPU", "vram_total": 0, "vram_free": 0}
    index = torch.cuda.current_device()
    free, total = torch.cuda.mem_get_info(index)
    return {
        "device": torch.cuda.get_device_name(index),
        "vram_total": total,
        "vram_free": free,
    }


def _write_local_registration(payload):
    directory = _local_registry_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{payload['id']}.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(target)


def _safe_peer(peer, now):
    if not isinstance(peer, dict):
        return None
    peer_id, host, port = peer.get("id"), peer.get("host"), peer.get("port")
    if not isinstance(peer_id, str) or not peer_id or len(peer_id) > 128:
        return None
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        return None
    if not isinstance(host, str) or not host:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if not (address.is_private or address.is_loopback):
        return None
    if peer.get("protocol") not in (None, _PROTOCOL_VERSION):
        return None

    def count(value):
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            else 0
        )

    sanitized = {
        "id": peer_id,
        "name": str(peer.get("name") or host)[:64],
        "host": host,
        "port": port,
        "queue": count(peer.get("queue")),
        "running": count(peer.get("running")),
        "hardware": peer.get("hardware")
        if isinstance(peer.get("hardware"), dict)
        else {},
        "protocol": peer.get("protocol", _PROTOCOL_VERSION),
        "allow_incoming_shared": peer.get("allow_incoming_shared")
        if isinstance(peer.get("allow_incoming_shared"), bool)
        else None,
        "last_seen": now,
    }
    return sanitized


def _local_peers(identity, now):
    peers = []
    for path in _local_registry_dir().glob("*.json"):
        try:
            peer = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(peer, dict) or peer.get("id") == identity:
            continue
        if now - peer.get("seen", 0) >= _STALE_SECONDS:
            continue
        sanitized = _safe_peer({**peer, "host": "127.0.0.1"}, now)
        if sanitized:
            sanitized["last_seen"] = peer.get("seen", now)
            peers.append(sanitized)
    return peers


def _remove_local_registration():
    identity = _identity()
    if identity is None:
        return
    try:
        (_local_registry_dir() / f"{identity}.json").unlink(missing_ok=True)
    except OSError:
        pass


def _local_registration_loop():
    registered = False
    while True:
        try:
            if _shared_enabled():
                payload = _status_payload()
                if payload is not None:
                    _write_local_registration(payload)
                    registered = True
            elif registered:
                _remove_local_registration()
                registered = False
        except Exception as error:
            print("[ComfyUI_GXNodes] local registry failed: %s" % error)
        time.sleep(2)


def _beacon():
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("", _PORT))
    except OSError as error:
        print("[ComfyUI_GXNodes] UDP discovery unavailable: %s" % error)
        sender.close()
        listener.close()
        return
    listener.settimeout(1.0)
    last = 0.0
    while True:
        now = time.time()
        if not _shared_enabled():
            with _lock:
                _peers.clear()
            time.sleep(2)
            continue
        try:
            payload = _status_payload()
            if payload is None:
                time.sleep(2)
                continue
        except Exception:
            time.sleep(2)
            continue
        if now - last >= 2:
            try:
                sender.sendto(json.dumps(payload).encode(), ("<broadcast>", _PORT))
            except OSError:
                pass
            last = now
        try:
            data, _address = listener.recvfrom(4096)
            peer = json.loads(data.decode())
            if isinstance(peer, dict) and peer.get("id") != payload["id"]:
                sanitized = _safe_peer(peer, time.time())
                if sanitized:
                    with _lock:
                        _peers[sanitized["id"]] = sanitized
        except (socket.timeout, ValueError, UnicodeError, OSError):
            pass


def _normalize_target(value):
    """Return a canonical ``http://host:port`` origin for a LAN target, or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlparse(value.strip().rstrip("/"))
        hostname, port = parsed.hostname, parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in ("http", "https")
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment or port == 0:
        return None
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        if parsed.hostname not in ("localhost", socket.gethostname()):
            return None
    else:
        if not (address.is_private or address.is_loopback):
            return None
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}:{port}"


def _target_dir_name(value):
    name = "".join(
        char if char.isalnum() or char in "-._" else "-" for char in str(value or "")
    )[:64]
    return name or "peer"


def _target_name(target):
    parsed = urlparse(target)
    return _target_dir_name(parsed.hostname or "peer")


def _standard_directories():
    return {
        "input": Path(folder_paths.get_input_directory()).resolve(),
        "output": _output_root(),
        "temp": Path(folder_paths.get_temp_directory()).resolve(),
    }


def _is_within(directory, candidate):
    try:
        return Path(candidate).resolve().is_relative_to(Path(directory).resolve())
    except (OSError, ValueError):
        return False


class _ResourceTooLarge(Exception):
    """A local resource exceeded the single-file limit while being read."""


def _sha256(path, limit=None):
    limit = _MAX_RESOURCE_BYTES if limit is None else limit
    digest, total = hashlib.sha256(), 0
    with open(path, "rb") as source:
        while chunk := source.read(min(1024 * 1024, limit + 1 - total)):
            total += len(chunk)
            if total > limit:
                raise _ResourceTooLarge
            digest.update(chunk)
    return digest.hexdigest(), total


class _ResourceScanError(Exception):
    """A local resource path could not be safely inspected."""


def _parse_resource_reference(value):
    """Parse Comfy annotations once, then classify the actual path without I/O.

    Unknown strings are path-shaped only when component edges have no whitespace;
    spaces inside a filename are allowed. This leaves prose such as '../ 表示上级'
    untouched, without probing it. Declared resources still require the full wire grammar.
    """
    relative, annotated_base = folder_paths.annotated_filepath(value)
    lexical = relative.replace("\\", "/")
    parts = lexical.split("/")
    path_shaped = all(part == part.strip() for part in parts)
    unsafe = (
        "\x00" in relative
        or lexical.startswith("/")
        or bool(ntpath.splitdrive(relative)[0])
        or (path_shaped and ".." in parts)
    )
    if os.name == "nt":
        relative = lexical
    # On POSIX a backslash can be a literal filename character. An unknown field
    # may be checked for ambiguity using its original name, never a renamed path.
    inspectable = _safe_relative_path(relative) or (
        os.name != "nt" and "\\" in relative and _safe_relative_path(lexical)
    )
    return relative, annotated_base, unsafe, inspectable


def _resource_for_value(value, pointer, node_id, remaining=None):
    remaining = _MAX_RESOURCE_BYTES if remaining is None else remaining

    def blocked(message):
        return {"node_id": str(node_id), "pointer": pointer, "error": message}

    if not isinstance(value, str) or not value:
        return blocked("素材输入必须是单个文件引用")
    try:
        relative, annotated_base, unsafe, _inspectable = _parse_resource_reference(
            value
        )
    except (AttributeError, ValueError):
        return blocked("素材路径注解无效")
    if unsafe or not _safe_relative_path(relative):
        return blocked("素材必须使用可跨平台的标准目录相对路径")
    try:
        directories = _standard_directories()
        directory_type = next(
            (
                name
                for name, path in directories.items()
                if annotated_base and Path(annotated_base).resolve() == path
            ),
            None if annotated_base else "input",
        )
        if directory_type is None:
            return blocked("素材目录注解不受支持")
        base = directories[directory_type]
        path = (base / relative).resolve()
        if not _is_within(base, path):
            return blocked("素材链接指向标准目录以外")
        if not path.is_file():
            return blocked("素材文件不存在或不是普通文件")
        size = path.stat().st_size
        if size > _MAX_RESOURCE_BYTES:
            return {
                "node_id": str(node_id),
                "pointer": pointer,
                "error": "单个资源超过 1 GB 上限",
            }
        if size > remaining:
            return {
                "node_id": str(node_id),
                "pointer": pointer,
                "error": "本次资源总量超过 1 GB 上限",
            }
        digest, actual_size = _sha256(path, remaining)
        if actual_size != size:
            return {
                "node_id": str(node_id),
                "pointer": pointer,
                "error": "素材在扫描期间发生变化，请重新确认",
            }
    except _ResourceTooLarge:
        message = (
            "单个资源超过 1 GB 上限"
            if remaining == _MAX_RESOURCE_BYTES
            else "素材在扫描期间发生变化，请重新确认"
        )
        return {"node_id": str(node_id), "pointer": pointer, "error": message}
    except (OSError, ValueError) as error:
        raise _ResourceScanError from error
    return {
        "id": digest,
        "node_id": str(node_id),
        "pointer": pointer,
        "filename": Path(relative).name,
        "relative_path": relative,
        "directory_type": directory_type,
        "size": size,
        "large": size > _LARGE_RESOURCE_BYTES,
        "sha256": digest,
        "_path": str(path),
    }


def _walk_resources(value, pointer, node_id, blocked, depth=0):
    """Unknown fields are never migrated; report actual file ambiguity only."""
    if depth > 32:
        blocked.append(
            {
                "node_id": str(node_id),
                "pointer": pointer,
                "error": "prompt 嵌套层级过深",
            }
        )
        return
    if isinstance(value, str) and value:

        def reject(message):
            blocked.append(
                {"node_id": str(node_id), "pointer": pointer, "error": message}
            )

        try:
            relative, annotated_base, unsafe, inspectable = _parse_resource_reference(
                value
            )
            if unsafe:
                reject("该节点字段尚未支持标准目录以外或无效的路径输入")
                return
            if not inspectable:
                return
            directories = _standard_directories()
            base = (
                Path(annotated_base).resolve()
                if annotated_base
                else directories["input"]
            )
            if base not in directories.values():
                reject("输入引用了不支持的目录")
                return
            path = (base / relative).resolve()
            if not _is_within(base, path):
                reject("输入链接指向标准目录以外")
                return
            matched = path.is_file()
        except (AttributeError, OSError, ValueError):
            reject("无法检查节点输入的本地文件引用")
            return
        if matched:
            blocked.append(
                {
                    "node_id": str(node_id),
                    "pointer": pointer,
                    "error": "输入匹配本地文件，但该节点字段尚未支持素材迁移；不会自动上传或改写",
                }
            )
    elif isinstance(value, dict):
        for key, item in value.items():
            _walk_resources(item, pointer + [key], node_id, blocked, depth + 1)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk_resources(item, pointer + [index], node_id, blocked, depth + 1)


def _scan_resources_sync(prompt):
    resources, blocked = [], []
    remaining = _MAX_RESOURCE_BYTES
    text_only_fields = _active_text_only_fields()
    for node_id, node in prompt.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        node_class = nodes.NODE_CLASS_MAPPINGS.get(class_type)
        declared = {}
        if node_class is not None:
            try:
                declared = _declared_inputs(node_class)
            except Exception:
                blocked.append(
                    {
                        "node_id": str(node_id),
                        "pointer": [node_id, "inputs"],
                        "error": "无法读取节点输入定义，请检查节点版本后重试",
                    }
                )
                continue
        for name, value in node.get("inputs", {}).items():
            pointer = [node_id, "inputs", name]
            if (
                isinstance(value, list)
                and len(value) == 2
                and isinstance(value[0], str)
                and value[0] in prompt
                and type(value[1]) is int
                and value[1] >= 0
            ):
                continue
            if name in text_only_fields.get(class_type, ()) and isinstance(value, str):
                continue
            if not _is_resource_input(node_class, name, declared.get(name)):
                _walk_resources(value, pointer, node_id, blocked)
                continue
            candidate = _resource_for_value(value, pointer, node_id, remaining)
            if candidate.get("error"):
                blocked.append(candidate)
                return resources, blocked, sum(item["size"] for item in resources)
            resources.append(candidate)
            remaining -= candidate["size"]
    return resources, blocked, sum(item["size"] for item in resources)


async def _scan_resources(prompt):
    return await asyncio.to_thread(_scan_resources_sync, prompt)


def _public_resource(resource):
    return {key: value for key, value in resource.items() if key != "_path"}


_RESOURCE_PUBLIC_KEYS = frozenset(
    {
        "id",
        "sha256",
        "node_id",
        "pointer",
        "filename",
        "relative_path",
        "directory_type",
        "size",
        "large",
    }
)
_RESOURCE_REQUIRED_KEYS = _RESOURCE_PUBLIC_KEYS - {"large"}


def _resource_contract(resource):
    """Strict, hashable projection of a public resource for confirmation checks."""
    if not isinstance(resource, dict):
        raise ValueError("资源描述符类型无效")
    keys = set(resource)
    if not _RESOURCE_REQUIRED_KEYS.issubset(keys) or not keys.issubset(
        _RESOURCE_PUBLIC_KEYS
    ):
        raise ValueError("资源描述符字段无效")
    for key in (
        "id",
        "sha256",
        "node_id",
        "filename",
        "relative_path",
        "directory_type",
    ):
        if not isinstance(resource[key], str) or not resource[key]:
            raise ValueError("资源描述符字段无效")
    if (
        not _safe_relative_path(resource["relative_path"])
        or not _safe_filename(resource["filename"])
        or resource["relative_path"].split("/")[-1] != resource["filename"]
        or resource["directory_type"] not in {"input", "output", "temp"}
    ):
        raise ValueError("资源路径无效")
    digest = resource["sha256"]
    if (
        resource["id"] != digest
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise ValueError("资源摘要无效")
    pointer = resource["pointer"]
    if (
        not isinstance(pointer, list)
        or not 3 <= len(pointer) <= 34
        or pointer[0] != resource["node_id"]
        or pointer[1] != "inputs"
    ):
        raise ValueError("资源输入位置无效")
    for part in pointer:
        if isinstance(part, bool) or not isinstance(part, (str, int)):
            raise ValueError("资源输入位置无效")
        if isinstance(part, int) and part < 0:
            raise ValueError("资源输入位置无效")
    size = resource["size"]
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or not 0 <= size <= _MAX_RESOURCE_BYTES
    ):
        raise ValueError("资源大小无效")
    if "large" in resource and not isinstance(resource["large"], bool):
        raise ValueError("资源描述符字段无效")
    return (
        resource["id"],
        digest,
        resource["node_id"],
        tuple(pointer),
        resource["filename"],
        resource["relative_path"],
        resource["directory_type"],
        size,
    )


def _resource_counter(resources):
    resources = list(resources)
    if len(resources) > _MAX_RESOURCE_REFS:
        raise ValueError("资源引用数量过多")
    return Counter(_resource_contract(resource) for resource in resources)


def _set_pointer(document, pointer, value):
    target = document
    for key in pointer[:-1]:
        target = target[key]
    target[pointer[-1]] = value


def _safe_job_id(value):
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 64
        and all(char.isalnum() or char in "-_" for char in value)
    )


def _safe_filename(value):
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or value[-1] in " ."
        or any(ord(char) < 32 or char in '/\\:<>"|?*' for char in value)
    ):
        return False
    stem = value.split(".", 1)[0].upper()
    return stem not in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} and not (
        len(stem) == 4 and stem[:3] in {"COM", "LPT"} and stem[3] in "123456789¹²³"
    )


def _safe_relative_path(value):
    return (
        isinstance(value, str)
        and bool(value)
        and all(_safe_filename(part) for part in value.split("/"))
    )


def _asset_directory(directory_type, job_id):
    directory = _standard_directories().get(directory_type)
    if directory is None or not _safe_job_id(job_id):
        raise ValueError("无效资源目录")
    result = directory / "GXShared" / job_id
    result.mkdir(parents=True, exist_ok=True)
    return result


def _reference_for_asset(directory_type, job_id, relative_path):
    relative = f"GXShared/{job_id}/{relative_path.replace('\\', '/')}"
    return relative if directory_type == "input" else f"{relative} [{directory_type}]"


def _custom_validated_inputs(node_class):
    """Return None when static preflight must defer all inputs to the node."""
    validator = getattr(node_class, "validate_inputs", None)
    if validator is not None:
        from comfy_api.latest import io

        # The public V3 base declares an optional placeholder, not a validator.
        if issubclass(node_class, io.ComfyNode):
            base = getattr(io.ComfyNode, "validate_inputs", None)
            if getattr(validator, "__func__", validator) is getattr(
                base, "__func__", base
            ):
                return ()
        else:
            validator = None
    if validator is None:
        validator = getattr(node_class, "VALIDATE_INPUTS", None)
        if validator is None:
            return ()
    try:
        arguments = inspect.getfullargspec(validator)
    except (TypeError, ValueError):
        return None
    return None if arguments.varkw is not None else arguments.args


def _node_preflight(prompt):
    missing_nodes, missing_models = [], []
    for node_id, node in prompt.items():
        if not isinstance(node, dict):
            continue
        node_class = nodes.NODE_CLASS_MAPPINGS.get(node.get("class_type"))
        if node_class is None:
            missing_nodes.append(
                {"node_id": str(node_id), "class_type": node.get("class_type")}
            )
            continue
        try:
            declared = _declared_inputs(node_class)
        except Exception:
            missing_nodes.append(
                {
                    "node_id": str(node_id),
                    "class_type": node.get("class_type"),
                    "error": "无法读取节点输入定义",
                }
            )
            continue
        custom_inputs = _custom_validated_inputs(node_class)
        if custom_inputs is None:
            continue
        for name, value in node.get("inputs", {}).items():
            config = declared.get(name)
            if (
                name in custom_inputs
                or not isinstance(value, str)
                or not isinstance(config, (tuple, list))
                or not config
            ):
                continue
            if _is_resource_input(node_class, name, config):
                continue
            metadata = (
                config[1] if len(config) > 1 and isinstance(config[1], dict) else {}
            )
            if metadata.get("remote"):
                continue
            input_type = config[0]
            options = metadata.get("options") if input_type == "COMBO" else input_type
            if isinstance(options, list) and value not in options:
                missing_models.append(
                    {"node_id": str(node_id), "input": name, "value": value}
                )
    return missing_nodes, missing_models


def _peer_headers():
    token = gxsettings.effective_token()
    return {"X-GX-Token": token} if token else {}


def _decode_json(raw):
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return None
    return data


class _RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_no_redirect(request, timeout):
    return build_opener(ProxyHandler({}), _RejectRedirect()).open(
        request, timeout=timeout
    )


class _RemoteResponseError(RuntimeError):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


async def _remote_json(target, path, payload):
    timeout = aiohttp.ClientTimeout(total=_REMOTE_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
        # Some ComfyUI extensions inject a proxy when this request keyword is absent.
        async with session.post(
            target + path,
            json=payload,
            headers=_peer_headers(),
            proxy=None,
            allow_redirects=False,
        ) as response:
            if 300 <= response.status < 400:
                raise _RemoteResponseError(
                    f"目标返回重定向，已拒绝 HTTP {response.status}", response.status
                )
            raw = await response.text()
            data = _decode_json(raw)
            if response.status >= 400:
                detail = (
                    data.get("error") or data.get("detail")
                    if isinstance(data, dict)
                    else None
                )
                raise _RemoteResponseError(
                    detail or raw[:1000] or f"HTTP {response.status}", response.status
                )
            if not isinstance(data, dict):
                raise RuntimeError(
                    f"目标返回了无效响应：HTTP {response.status} {raw[:1000]}"
                )
            return data


class _HashingPayload(Payload):
    """Hash the exact bytes written in one multipart upload, with a completion signal."""

    _autoclose = True

    def __init__(self, source, expected_size, expected_hash, filename):
        super().__init__(
            source, content_type="application/octet-stream", filename=filename
        )
        self._source = source
        self._expected_size = expected_size
        self._expected_hash = expected_hash
        self._size = expected_size
        self.digest = hashlib.sha256()
        self.total = 0
        self.completed = False
        self.error = None
        self._started = False
        self._pending_read = None
        self._cleanup_task = None
        self._closing = False

    async def _read(self, size):
        if self._closing:
            raise asyncio.CancelledError()
        self._pending_read = asyncio.create_task(
            asyncio.to_thread(self._source.read, size)
        )
        chunk = await asyncio.shield(self._pending_read)
        if self._closing:
            raise asyncio.CancelledError()
        return chunk

    def _close(self):
        if self._closing:
            return
        self._closing = True
        pending, source = self._pending_read, self._source

        async def release():
            if pending is not None:
                try:
                    await asyncio.shield(pending)
                except (Exception, asyncio.CancelledError):
                    pass
            try:
                await asyncio.to_thread(source.close)
            except OSError:
                print("[ComfyUI_GXNodes] 素材快照关闭失败，请检查临时存储")

        self._cleanup_task = asyncio.create_task(release())

    @property
    def size(self):
        return self._size

    def decode(self, encoding="utf-8", errors="strict"):
        raise TypeError("streaming upload payload is not decodable")

    async def write(self, writer):
        await self.write_with_length(writer, None)

    async def write_with_length(self, writer, content_length):
        if self._started:
            raise RuntimeError("上传请求体不能重放，请重新确认")
        self._started = True
        limit = (
            self._expected_size
            if content_length is None
            else min(self._expected_size, content_length)
        )
        remaining = limit
        while remaining > 0:
            chunk = await self._read(min(65536, remaining + 1))
            if not chunk:
                break
            if len(chunk) > remaining:
                self.error = "素材在传输期间发生变化，请重新确认"
                chunk = chunk[:remaining]
            self.total += len(chunk)
            self.digest.update(chunk)
            await writer.write(chunk)
            remaining -= len(chunk)
        if remaining == 0:
            extra = await self._read(1)
            if extra:
                self.error = "素材在传输期间发生变化，请重新确认"
        if (
            self.total != self._expected_size
            or self.digest.hexdigest() != self._expected_hash
        ):
            self.error = "素材在传输期间发生变化，请重新确认"
        if not self.error:
            self.completed = True


async def _upload_resource(target, job_id, resource, snapshot):
    form = aiohttp.FormData()
    form.add_field("job_id", job_id)
    form.add_field("resource_id", resource["id"])
    form.add_field("sha256", resource["sha256"])
    form.add_field("size", str(resource["size"]))
    form.add_field("directory_type", resource["directory_type"])
    form.add_field("filename", resource["filename"])
    form.add_field("relative_path", resource["relative_path"])
    payload = _HashingPayload(
        snapshot.source, resource["size"], resource["sha256"], resource["filename"]
    )
    snapshot.take()  # payload is now the sole owner; no pathname reopen.
    cancelled = False
    try:
        form.add_field("file", payload, filename=resource["filename"])
        timeout = aiohttp.ClientTimeout(
            total=max(_REMOTE_TIMEOUT_SECONDS, resource["size"] / (1024 * 1024) * 4)
        )
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.post(
                target + "/gx/shared/assets",
                data=form,
                headers=_peer_headers(),
                proxy=None,
                allow_redirects=False,
            ) as response:
                if 300 <= response.status < 400:
                    raise RuntimeError(f"目标返回重定向，已拒绝 HTTP {response.status}")
                raw = await response.text()
                data = _decode_json(raw)
                if response.status >= 400:
                    detail = data.get("error") if isinstance(data, dict) else None
                    raise RuntimeError(
                        detail or raw[:1000] or f"上传资源失败 HTTP {response.status}"
                    )
                if not isinstance(data, dict) or not data.get("reference"):
                    raise RuntimeError(
                        f"目标上传响应无效：HTTP {response.status} {raw[:1000]}"
                    )
                if not payload.completed:
                    raise RuntimeError(
                        payload.error or "上传请求体未完整写出，请重新确认"
                    )
                return data["reference"]
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        payload._close()
        if not cancelled:
            await asyncio.shield(payload._cleanup_task)


def _save_job(job, *, accepted_submission=False):
    """Persist an independent snapshot; failure stops callers, never retries writes."""
    snapshot = copy.deepcopy(job)
    serialized = json.dumps(snapshot, ensure_ascii=False, indent=2)
    path = _jobs_dir() / f"{snapshot['id']}.json"
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{snapshot['id']}-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(serialized)
        with _lock:
            temporary.replace(path)
            _jobs[snapshot["id"]] = snapshot
        return True
    except OSError:
        conservative = copy.deepcopy(snapshot)
        conservative["status"] = "failed"
        conservative.pop("completed_at", None)
        conservative["error"] = (
            "任务保存状态未确认，可能已有远端任务或部分结果；请核对，不会自动重试"
        )
        if accepted_submission:
            conservative.update(
                error_code="accepted_save_unconfirmed", error=_ACCEPTED_SAVE_UNCONFIRMED
            )
        try:
            persisted = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(persisted, dict)
                and persisted.get("id") == snapshot["id"]
                and persisted.get("status") == "completed"
                and isinstance(persisted.get("outputs"), list)
                and all(isinstance(value, str) for value in persisted["outputs"])
            ):
                conservative = persisted
        except (OSError, ValueError):
            pass
        with _lock:
            _jobs[snapshot["id"]] = conservative
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                print("[ComfyUI_GXNodes] 无法清理本次任务记录临时文件")


def _workflow_name(value):
    if isinstance(value, str) and len(value) <= _MAX_WORKFLOW_NAME:
        if not any(
            unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"}
            or unicodedata.bidirectional(char)
            in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}
            for char in value
        ):
            return value.strip() or None
    return None


def _valid_remote_prompt_id(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 256
        and value.isascii()
        and all(char.isalnum() or char in "-_" for char in value)
    )


def _public_job(job, *, include_target_address=False):
    public = {
        key: value
        for key, value in job.items()
        if key not in {"target", "resources", "target_address", "workflow_name"}
    }
    if (
        job.get("status") == "failed"
        and not job.get("error_code")
        and job.get("error") == _SUBMISSION_UNKNOWN
    ):
        public["error_code"] = "submission_unknown"
    if name := _workflow_name(job.get("workflow_name")):
        public["workflow_name"] = name
    if include_target_address and (target := _normalize_target(job.get("target"))):
        public["target_address"] = target
    if job.get("status") == "completed":
        outputs = job.get("outputs") or []
        public["outputs_available"] = bool(outputs) and all(
            Path(path).is_file() for path in outputs
        )
    return public


class _InvalidRemoteHistory(Exception):
    pass


def _remote_job_status(target, remote_id):
    request = Request(
        target + "/api/jobs/" + quote(remote_id, safe=""), headers=_peer_headers()
    )
    try:
        with _open_no_redirect(
            request, timeout=_JOB_STATUS_TIMEOUT_SECONDS
        ) as response:
            raw = response.read(_MAX_JOB_STATUS_BYTES + 1)
        if len(raw) > _MAX_JOB_STATUS_BYTES:
            return None
        data = _decode_json(raw)
    except HTTPError as error:
        with error:
            if error.code == 404:
                try:
                    missing = _decode_json(error.read(512))
                except (OSError, http.client.HTTPException):
                    missing = None
                if (
                    isinstance(missing, dict)
                    and missing.get("error") == "Job not found"
                ):
                    return "not_found"
        if 300 <= error.code < 600:
            return None
        raise
    except (URLError, OSError, ValueError, http.client.HTTPException, RecursionError):
        # The native jobs route is optional. History is still authoritative for recovery.
        return None
    if isinstance(data, dict) and data.get("id") == remote_id:
        status = data.get("status")
        if status in ("pending", "in_progress", "completed", "failed", "cancelled"):
            return status
    return None


def _remote_history_entry(target, remote_id):
    request = Request(
        target + "/history/" + quote(remote_id, safe=""), headers=_peer_headers()
    )
    with _open_no_redirect(request, timeout=_REMOTE_TIMEOUT_SECONDS) as response:
        raw_history = response.read()
    try:
        history = json.loads(raw_history.decode())
    except (ValueError, UnicodeError, RecursionError) as error:
        raise _InvalidRemoteHistory() from error
    if not isinstance(history, dict):
        raise _InvalidRemoteHistory()
    if remote_id not in history:
        return None
    entry = history[remote_id]
    if not isinstance(entry, dict) or (
        entry.get("status") is not None and not isinstance(entry["status"], dict)
    ):
        raise _InvalidRemoteHistory()
    return entry


def _save_monitor_state(job, status, error_code=None, error=None):
    if job["status"] == status and job.get("error_code") == error_code:
        return True
    job["status"] = status
    job.pop("error", None)
    job.pop("error_code", None)
    if error_code is not None:
        job["error_code"] = error_code
    if error is not None:
        job["error"] = error
    return _save_job(job)


def _media_entries(value, results):
    if isinstance(value, dict):
        media_type = value.get("type")
        if (
            isinstance(value.get("filename"), str)
            and isinstance(media_type, str)
            and media_type in {"input", "output", "temp"}
        ):
            filename, subfolder = value["filename"], value.get("subfolder", "")
            if subfolder is None:
                subfolder = ""
            if (
                "\x00" in filename
                or not _safe_filename(filename)
                or not _safe_subfolder(subfolder)
            ):
                raise _InvalidRemoteHistory()
            results.append(
                {"filename": filename, "subfolder": subfolder, "type": media_type}
            )
        for item in value.values():
            _media_entries(item, results)
    elif isinstance(value, list):
        for item in value:
            _media_entries(item, results)


def _safe_subfolder(value):
    if not isinstance(value, str) or "\x00" in value:
        return False
    path = Path(value or ".")
    return not path.is_absolute() and ".." not in path.parts


def _download_media(target, job, media):
    filename, subfolder = media["filename"], media.get("subfolder", "")
    if not _safe_filename(filename) or not _safe_subfolder(subfolder):
        raise ValueError("远端返回了不安全的输出路径")
    query = urlencode(
        {"filename": filename, "subfolder": subfolder, "type": media["type"]}
    )
    base = _output_root()
    root = base / "GXShared"
    output = root / _target_dir_name(job.get("target_name")) / job["id"]
    if (
        not _safe_job_id(job.get("id"))
        or not _is_within(base, root)
        or not _is_within(root, output)
    ):
        raise ValueError("共享结果目录无效")
    output.mkdir(parents=True, exist_ok=True)
    if not _is_within(base, root) or not _is_within(root, output):
        raise ValueError("共享结果目录无效")
    request = Request(target + "/view?" + query, headers=_peer_headers())
    temporary = None
    try:
        with _open_no_redirect(request, timeout=_REMOTE_TIMEOUT_SECONDS) as response:
            length = response.headers.get("Content-Length")
            if length is not None:
                if not length.isascii() or not length.isdecimal():
                    raise ValueError("远端结果长度无效")
                length = int(length)
                if length > _MAX_RESOURCE_BYTES:
                    raise ValueError("远端结果超过 1 GB 上限")
            written = 0
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".gx-download-",
                suffix=".tmp",
                dir=output,
                delete=False,
            ) as sink:
                temporary = Path(sink.name)
                while chunk := response.read(1024 * 1024):
                    written += len(chunk)
                    if written > _MAX_RESOURCE_BYTES:
                        raise ValueError("远端结果超过 1 GB 上限")
                    sink.write(chunk)
            if length is not None and written != length:
                raise ValueError("远端结果传输不完整")
        suffix = 0
        while True:
            destination = output / (
                filename
                if not suffix
                else f"{Path(filename).stem}_{suffix}{Path(filename).suffix}"
            )
            try:
                if os.name == "nt":
                    # Windows rename fails when destination exists, including races.
                    os.rename(temporary, destination)
                    temporary = None
                else:
                    os.link(temporary, destination)
                break
            except FileExistsError:
                suffix += 1
        return str(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _monitor_job(job_id):
    failures = 0
    partial_error = (
        "结果下载已停止，可能已有部分文件保存；请核对共享结果目录，不会自动重复下载"
    )
    while True:
        with _lock:
            job = copy.deepcopy(_jobs.get(job_id))
        if not job or job["status"] in {"completed", "failed"}:
            return
        if job["status"] == "downloading":
            # Recovery owns interrupted downloads; never launch a second publisher.
            return
        download_boundary = False
        try:
            remote_id = job.get("remote_prompt_id")
            if not _valid_remote_prompt_id(remote_id):
                _save_monitor_state(
                    job, "failed", "submission_unknown", _SUBMISSION_UNKNOWN
                )
                return
            native_status = _remote_job_status(job["target"], remote_id)
            if native_status in ("pending", "in_progress"):
                failures = 0
                if not _save_monitor_state(
                    job, "queued" if native_status == "pending" else "executing"
                ):
                    return
                time.sleep(2)
                continue
            native_failed = native_status in ("failed", "cancelled")
            try:
                entry = _remote_history_entry(job["target"], remote_id)
            except (
                _InvalidRemoteHistory,
                URLError,
                OSError,
                ValueError,
                http.client.HTTPException,
            ):
                if not native_failed:
                    raise
                entry = None
            status_data = (entry or {}).get("status") or {}
            status = status_data.get("status_str")
            if native_failed or status == "error":
                cancelled = native_status == "cancelled"
                code = "remote_cancelled" if cancelled else "remote_failed"
                message = "远端已取消任务" if cancelled else "远端任务失败"
                _save_monitor_state(
                    job, "failed", code, status_data.get("messages") or message
                )
                return
            if (
                entry is None
                or status_data.get("completed") is False
                or status not in (None, "", "success")
            ):
                failures = 0
                known_native = native_status in ("completed", "not_found")
                error_code = "remote_status_unknown" if known_native else None
                error = "暂无法核实远端状态，继续等待结果" if known_native else None
                if not _save_monitor_state(job, "running", error_code, error):
                    return
                time.sleep(2)
                continue
            # Once this durable boundary is attempted, never replay publication.
            # A failed save may already have replaced the on-disk record.
            download_boundary = True
            # Only a confirmed boundary permits result publication.
            if not _save_monitor_state(job, "downloading"):
                return
            media = []
            try:
                _media_entries(entry.get("outputs", {}), media)
            except RecursionError as error:
                raise _InvalidRemoteHistory() from error
            job["outputs"] = []
            for item in media:
                job["outputs"].append(_download_media(job["target"], job, item))
            job["status"] = "completed"
            job["completed_at"] = time.time()
            _save_job(job)
            return
        except _InvalidRemoteHistory:
            _save_monitor_state(
                job, "failed", "invalid_remote_history", "远端任务历史格式无效"
            )
            return
        except (
            HTTPError,
            URLError,
            OSError,
            ValueError,
            http.client.HTTPException,
        ) as error:
            if download_boundary:
                _save_monitor_state(
                    job, "failed", "result_download_interrupted", partial_error
                )
                return
            failures += 1
            if failures >= _MONITOR_RETRIES:
                _save_monitor_state(job, "failed", "monitor_disconnected", str(error))
                return
            if not _save_monitor_state(
                job,
                job["status"],
                "monitor_disconnected",
                "暂时无法连接远端，正在重新检查",
            ):
                return
            time.sleep(min(2**failures, 30))


def _start_monitor(job_id):
    with _lock:
        if job_id in _monitors:
            return
        _monitors.add(job_id)

    def run():
        try:
            _monitor_job(job_id)
        finally:
            with _lock:
                _monitors.discard(job_id)

    threading.Thread(target=run, name="gx-shared-monitor", daemon=True).start()


def _restore_jobs(force=False):
    global _restored
    if not _shared_enabled():
        return
    with _lock:
        if _restored and not force:
            return
        _restored = True
        previous = dict(_jobs)
    try:
        restored = []
        for path in _jobs_dir().glob("*.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if job.get("id"):
                restored.append(job)
        restored.sort(key=lambda item: item.get("created_at", 0), reverse=True)
        restored = restored[:20]
        restored_ids = {job["id"] for job in restored}
        with _lock:
            for job_id, job in previous.items():
                if (
                    job_id not in restored_ids
                    and _jobs.get(job_id) is job
                    and job.get("status") in {"completed", "failed"}
                ):
                    _jobs.pop(job_id)
            # A request may publish its first job while the startup scan is
            # reading disk. Keep the in-process publisher's state authoritative.
            restored = [job for job in restored if job["id"] not in _jobs]
            _jobs.update({job["id"]: job for job in restored})
        for job in restored:
            if job.get("status") == "downloading":
                _save_job(
                    dict(
                        job,
                        status="failed",
                        error="结果下载中断，可能已有部分文件保存；请核对共享结果目录，不会自动重复下载",
                    )
                )
                continue
            if job.get("status") == "uploading" and not job.get("remote_prompt_id"):
                _save_job(
                    dict(
                        job,
                        status="failed",
                        error_code="submission_unknown",
                        error=_SUBMISSION_UNKNOWN,
                    )
                )
                continue
            if job.get("status") not in {"completed", "failed"} and job.get(
                "remote_prompt_id"
            ):
                _start_monitor(job["id"])
    except (OSError, ValueError):
        pass


def _cleanup_directory(root, deadline):
    if not root.exists():
        return
    for path in root.iterdir():
        try:
            if path.is_dir() and path.stat().st_mtime < deadline:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _prune_expired_jobs(cutoff):
    jobs_dir = _jobs_dir()
    output_root = _output_root() / "GXShared"
    removed = 0
    for path in jobs_dir.glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if job.get("status") not in {"completed", "failed"}:
            continue
        timestamp = job.get("completed_at") or job.get("created_at") or 0
        if timestamp >= cutoff:
            continue
        target_name, job_id = job.get("target_name"), job.get("id")
        if isinstance(target_name, str) and target_name and _safe_job_id(job_id):
            result_dir = output_root / _target_dir_name(target_name) / job_id
            try:
                if result_dir.is_dir() and _is_within(output_root, result_dir):
                    shutil.rmtree(result_dir, ignore_errors=True)
            except OSError:
                pass
        try:
            path.unlink()
        except OSError:
            continue
        with _lock:
            _jobs.pop(job_id, None)
        removed += 1
    return removed


def _cleanup_expired(settings=None):
    settings = settings or gxsettings.retention_settings()
    now = time.time()
    transfer_hours = settings.get("transfer_retention_hours", 0)
    if transfer_hours > 0:
        deadline = now - transfer_hours * 3600
        for directory in (
            Path(folder_paths.get_input_directory()),
            Path(folder_paths.get_temp_directory()),
        ):
            _cleanup_directory(directory / "GXShared", deadline)
    retention_days = settings.get("result_retention_days", 0)
    if retention_days > 0:
        _prune_expired_jobs(now - retention_days * 86400)


def _cleanup_assets_loop():
    while True:
        try:
            _cleanup_expired()
        except Exception as error:
            print("[ComfyUI_GXNodes] shared cleanup failed: %s" % error)
        time.sleep(3600)


async def _peers_route(request):
    denied = _route_denied(request)
    if denied:
        return denied
    now, identity = time.time(), _identity()
    with _lock:
        peers = {
            item["id"]: item
            for item in _peers.values()
            if now - item.get("last_seen", 0) < _STALE_SECONDS
        }
    peers.update({item["id"]: item for item in _local_peers(identity, now)})
    return web.json_response({"peers": list(peers.values()), "self": _status_payload()})


async def _preflight_route(request):
    denied = _route_denied(request)
    if denied:
        return denied
    if not _incoming_allowed():
        return _incoming_denied()
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
    prompt = body.get("prompt") if isinstance(body, dict) else None
    if not isinstance(prompt, dict):
        return web.json_response({"error": "prompt 必须是对象"}, status=400)
    missing_nodes, missing_models = await asyncio.to_thread(_node_preflight, prompt)
    return web.json_response(
        {
            "ok": not missing_nodes and not missing_models,
            "protocol": _PROTOCOL_VERSION,
            "missing_nodes": missing_nodes,
            "missing_models": missing_models,
        }
    )


async def _prepare_route(request):
    denied = _route_denied(request)
    if denied:
        return denied
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
    target, prompt = _normalize_target(body.get("target")), body.get("prompt")
    if not target or not isinstance(prompt, dict):
        return web.json_response(
            {"error": "目标必须是局域网 HTTP 地址，且 prompt 必须是对象"}, status=400
        )
    try:
        resources, blocked, total = await _scan_resources(prompt)
    except _ResourceScanError:
        return web.json_response(
            {"error": "素材无法读取或路径无效，请检查后重试"}, status=400
        )
    try:
        preflight = await _remote_json(
            target, "/gx/shared/preflight", {"prompt": prompt}
        )
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as error:
        return web.json_response({"error": f"目标预检失败：{error}"}, status=502)
    if preflight.get("protocol") != _PROTOCOL_VERSION:
        blocked.append(
            {"error": "目标 GXNodes 版本不兼容，请同步三个 custom_nodes 后重启"}
        )
    if preflight.get("missing_nodes") or preflight.get("missing_models"):
        blocked.append({"error": "目标缺少节点或模型"})
    elif preflight.get("ok") is not True:
        blocked.append({"error": "目标预检未通过"})
    return web.json_response(
        {
            "ok": not blocked,
            "resources": [_public_resource(item) for item in resources],
            "total_size": total,
            "blocked": blocked,
            "preflight": preflight,
            "resource_contract": _RESOURCE_CONTRACT_VERSION,
        }
    )


async def _assets_route(request):
    denied = _route_denied(request)
    if denied:
        return denied
    if not _incoming_allowed():
        return _incoming_denied()
    try:
        reader, fields, file_part = await request.multipart(), {}, None
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "file":
                file_part = part
                break
            fields[part.name] = await part.text()
        job_id, filename = fields.get("job_id", ""), fields.get("filename", "")
        relative_path = fields.get("relative_path", "")
        directory_type, expected_hash = (
            fields.get("directory_type", ""),
            fields.get("sha256", ""),
        )
        try:
            expected_size = int(fields.get("size", "-1"))
        except ValueError:
            raise ValueError("资源大小或摘要无效")
        if (
            not _safe_job_id(job_id)
            or not _safe_filename(filename)
            or not _safe_relative_path(relative_path)
            or directory_type not in _standard_directories()
            or file_part is None
        ):
            raise ValueError("资源元数据无效")
        if (
            expected_size < 0
            or expected_size > _MAX_RESOURCE_BYTES
            or len(expected_hash) != 64
        ):
            raise ValueError("资源大小或摘要无效")
        parts = relative_path.split("/")
        if not parts or parts[-1] != filename:
            raise ValueError("资源路径无效")
        # Only the configured root may follow a Junction/symlink. Every child,
        # temporary write, publication and cleanup is bound to opened handles.
        with gxshared_receive.receive_root(
            _standard_directories()[directory_type]
        ) as root:
            with root.directory(["GXShared", job_id, *parts[:-1]]) as directory:
                with directory.create_temporary() as pending:
                    digest, total = hashlib.sha256(), 0
                    while chunk := await file_part.read_chunk(1024 * 1024):
                        total += len(chunk)
                        if total > _MAX_RESOURCE_BYTES:
                            raise ValueError("资源超过 1 GB 上限")
                        digest.update(chunk)
                        pending.write(chunk)
                    if total != expected_size or digest.hexdigest() != expected_hash:
                        raise ValueError("资源 SHA-256 或大小校验失败")
                    published_name = pending.publish_unique(filename)
        actual_relative_path = str(
            Path(relative_path).with_name(published_name)
        ).replace("\\", "/")
        return web.json_response(
            {
                "ok": True,
                "reference": _reference_for_asset(
                    directory_type, job_id, actual_relative_path
                ),
            }
        )
    except (ValueError, OSError) as error:
        return web.json_response({"error": str(error)}, status=400)


async def _push_route(request):
    denied = _route_denied(request)
    if denied:
        return denied
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
    target, prompt = _normalize_target(body.get("target")), body.get("prompt")
    approved = body.get("resources")
    contract_version = body.get("resource_contract")
    if not target or not isinstance(prompt, dict):
        return web.json_response(
            {"error": "目标必须是局域网 HTTP 地址，且 prompt 必须是对象"}, status=400
        )
    workflow_name = _workflow_name(body.get("workflow_name"))
    try:
        resources, blocked, _total = await _scan_resources(prompt)
    except _ResourceScanError:
        return web.json_response(
            {"error": "素材无法读取或路径无效，请检查后重试"}, status=400
        )
    if blocked:
        return web.json_response(
            {"error": "资源清单已变化或不符合传输限制，请重新确认"}, status=409
        )
    if contract_version != _RESOURCE_CONTRACT_VERSION or not isinstance(approved, list):
        return web.json_response(
            {"error": "资源确认合同已更新，请刷新页面后重新确认"}, status=409
        )
    try:
        approved_counter = _resource_counter(approved)
        current_counter = _resource_counter(
            _public_resource(item) for item in resources
        )
    except (ValueError, TypeError):
        return web.json_response(
            {"error": "资源清单已变化或不符合传输限制，请重新确认"}, status=409
        )
    if approved_counter != current_counter:
        return web.json_response(
            {"error": "资源清单已变化或不符合传输限制，请重新确认"}, status=409
        )
    job = None
    frozen = None
    submitting = False
    try:
        frozen = await gxshared_snapshot.snapshot_resources(
            resources, _MAX_RESOURCE_BYTES
        )
        preflight = await _remote_json(
            target, "/gx/shared/preflight", {"prompt": prompt}
        )
        if (
            preflight.get("protocol") != _PROTOCOL_VERSION
            or preflight.get("ok") is not True
            or preflight.get("missing_nodes")
            or preflight.get("missing_models")
        ):
            return web.json_response(
                {"error": "目标预检未通过", "detail": preflight}, status=409
            )
        job_id = uuid.uuid4().hex
        rewritten = copy.deepcopy(prompt)
        job = {
            "id": job_id,
            "target": target,
            "target_name": _target_name(target),
            "status": "uploading",
            "created_at": time.time(),
            "resources": [_public_resource(item) for item in resources],
            "outputs": [],
        }
        if workflow_name is not None:
            job["workflow_name"] = workflow_name
        if not _save_job(job):
            return web.json_response(
                {
                    "ok": False,
                    "error": "任务保存状态未确认，请核对后再操作",
                    "job_id": job_id,
                },
                status=503,
            )
        for item in frozen.items:
            resource = item.resource
            reference = await _upload_resource(target, job_id, resource, item)
            _set_pointer(rewritten, resource["pointer"], reference)
        submitting = True
        result = await _remote_json(
            target,
            "/prompt",
            {
                "prompt": rewritten,
                "extra_data": {"gx_shared": True, "gx_shared_job_id": job_id},
            },
        )
        remote_id = result.get("prompt_id")
        if not _valid_remote_prompt_id(remote_id):
            raise RuntimeError("目标未返回 prompt_id")
        job["remote_prompt_id"] = remote_id
        job["status"] = "queued"
        if not _save_job(job, accepted_submission=True):
            return web.json_response(
                {
                    "ok": False,
                    "error": _ACCEPTED_SAVE_UNCONFIRMED,
                    "code": "accepted_save_unconfirmed",
                    "job_id": job_id,
                },
                status=503,
            )
        _start_monitor(job_id)
        return web.json_response(
            {"ok": True, "job": _public_job(job), "result": result}
        )
    except gxshared_snapshot.SnapshotError as error:
        return web.json_response({"ok": False, "error": str(error)}, status=409)
    except asyncio.CancelledError:
        if job is not None:
            job.update(status="failed", error="任务在提交前取消")
            if submitting:
                job.update(error_code="submission_unknown", error=_SUBMISSION_UNKNOWN)
            _save_job(job)
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, OSError) as error:
        rejected = (
            isinstance(error, _RemoteResponseError)
            and 400 <= error.status < 500
            and error.status != 408
        )
        uncertain = submitting and not job.get("remote_prompt_id") and not rejected
        message = _SUBMISSION_UNKNOWN if uncertain else str(error)
        response = {"ok": False, "error": message}
        if uncertain:
            job["error_code"] = "submission_unknown"
            response.update(code=job["error_code"], job_id=job["id"])
        if job:
            job["status"], job["error"] = "failed", message
            if not _save_job(job):
                response.update(
                    error="任务保存状态未确认，请核对远端任务及共享结果，勿重复提交",
                    job_id=job["id"],
                )
                return web.json_response(response, status=503)
        return web.json_response(response, status=502)
    finally:
        if frozen is not None:
            # Only untouched snapshots remain here; payload owns transferred
            # handles until its pending read and deferred close complete.
            await asyncio.to_thread(frozen.close)


async def _jobs_route(request):
    denied = _route_denied(request)
    if denied:
        return denied
    _restore_jobs()
    with _lock:
        jobs = [
            _public_job(item, include_target_address=True)
            for item in sorted(
                _jobs.values(), key=lambda item: item.get("created_at", 0), reverse=True
            )[:20]
        ]
    return web.json_response({"jobs": jobs})


async def _settings_route(request):
    """Compatibility alias for the old retention settings endpoint."""
    denied = _route_denied(request)
    if denied:
        return denied
    if request.method == "GET":
        settings = gxsettings.retention_settings()
        settings["shared_token_set"] = bool(gxsettings.effective_token())
        return web.json_response({"settings": settings})
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)
    patch = {
        key: body[key]
        for key in ("result_retention_days", "transfer_retention_hours")
        if key in body
    }
    try:
        saved = gxsettings.save_settings(patch)
    except gxsettings.SettingsBusyError:
        return web.json_response(
            {"error": "设置正由其他实例保存，请稍后重试", "code": "gx_settings_busy"},
            status=409,
        )
    except (OSError, ValueError) as error:
        return web.json_response({"error": str(error)}, status=400)
    threading.Thread(target=_cleanup_expired, args=(saved,), daemon=True).start()
    return web.json_response({"ok": True, "settings": gxsettings.retention_settings()})


def register_shared_routes():
    global _started
    if _started:
        return
    # Register both controls before exposing the receiver switch.
    instance = PromptServer.instance
    if instance.app.frozen:
        raise RuntimeError("共享接单控制无法注册：服务应用已冻结")
    instance.add_on_prompt_handler(_guard_shared_prompt)
    instance.app.middlewares.append(_incoming_prompt_middleware)
    routes = PromptServer.instance.routes
    routes.get("/gx/shared/peers")(_peers_route)
    routes.post("/gx/shared/preflight")(_preflight_route)
    routes.post("/gx/shared/prepare")(_prepare_route)
    routes.post("/gx/shared/assets")(_assets_route)
    routes.post("/gx/shared/push")(_push_route)
    routes.get("/gx/shared/jobs")(_jobs_route)
    routes.get("/gx/shared/settings")(_settings_route)
    routes.post("/gx/shared/settings")(_settings_route)
    _started = True
    threading.Thread(
        target=_local_registration_loop, name="gx-local-registry", daemon=True
    ).start()
    threading.Thread(target=_beacon, name="gx-shared-beacon", daemon=True).start()
    threading.Thread(
        target=_restore_jobs, name="gx-shared-job-restore", daemon=True
    ).start()
    threading.Thread(
        target=_cleanup_assets_loop, name="gx-shared-asset-cleanup", daemon=True
    ).start()
    print(
        "[ComfyUI_GXNodes] shared compute protocol %s registered on UDP %s"
        % (_PROTOCOL_VERSION, _PORT)
    )
