"""Cross-site request protection for GXNodes' own aiohttp routes."""
import secrets
from urllib.parse import urlparse

from aiohttp import web

_GENERATED_TOKEN = secrets.token_urlsafe(32)
_LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
_started = False


def _settings_module():
    try:
        from . import gxsettings
    except ImportError:
        import gxsettings
    return gxsettings


def _configured_token():
    return str(_settings_module().effective_token() or "").strip()


def token():
    """Token handed to the bundled frontend for authenticated mutations."""
    return _configured_token() or _GENERATED_TOKEN


def token_is_configured():
    return bool(_configured_token())


def _same_origin(request):
    origin = request.headers.get("Origin")
    if origin:
        if origin == "null":
            return False
        host = request.headers.get("Host", "")
        if not host or urlparse(origin).netloc.lower() != host.lower():
            return False
    site = request.headers.get("Sec-Fetch-Site")
    if site and site not in ("same-origin", "none"):
        return False
    return True


def guard(request):
    """Return a 403 response for cross-site calls or a bad token when one is configured."""
    if not _same_origin(request):
        return web.json_response({"error": "请求被拒绝：不允许跨站调用", "code": "cross_origin"}, status=403)
    try:
        configured = _configured_token()
    except Exception:
        return web.json_response({"error": "GX 安全配置不可用", "code": "gx_security_unavailable"}, status=503)
    if configured:
        supplied = request.headers.get("X-GX-Token", "")
        if not secrets.compare_digest(supplied.encode("utf-8"), configured.encode("utf-8")):
            return web.json_response({"error": "请求被拒绝：缺少或无效的 GX 令牌", "code": "gx_auth_required"}, status=403)
    return None


async def _session_route(request):
    headers = {"Cache-Control": "no-store"}
    if not _same_origin(request):
        return web.json_response({"error": "请求被拒绝：不允许跨站调用", "code": "cross_origin"}, status=403, headers=headers)
    try:
        configured = _configured_token()
    except Exception:
        return web.json_response({"error": "GX 安全配置不可用", "code": "gx_security_unavailable"}, status=503, headers=headers)
    required = bool(configured)
    if request.remote in _LOOPBACK:
        return web.json_response({"token": configured or _GENERATED_TOKEN, "required": required}, headers=headers)
    supplied = request.headers.get("X-GX-Token", "")
    authenticated = not required or secrets.compare_digest(supplied.encode("utf-8"), configured.encode("utf-8"))
    return web.json_response({"required": required, "authenticated": authenticated}, headers=headers)


def register_security_routes():
    global _started
    if _started:
        return
    _started = True
    from server import PromptServer

    PromptServer.instance.routes.get("/gx/session")(_session_route)
