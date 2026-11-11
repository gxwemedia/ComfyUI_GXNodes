"""Bounded OpenAI-compatible text requests and explicitly selected local skills."""
import json
import base64
import io
import asyncio
import aiohttp
import os
import re
import yaml
from pathlib import Path
from urllib.parse import urlsplit

MAX_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 6 * 1024 * 1024
MAX_SKILL_BYTES = 128 * 1024

def agent_root():
    import server
    root = Path(server.__file__).resolve().parent.parent / '.comfypilot'
    if not root.is_dir():
        raise ValueError('当前 ComfyUI 发行目录缺少 .comfypilot，该目录用于存放 DSH 配置和默认技能。')
    if root.resolve() != root:
        raise ValueError('.comfypilot 必须位于当前发行目录，不允许链接到其他目录。')
    return root

class _UniqueSafeLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError('invalid or duplicate manifest key')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _local_skill_path(path):
    if os.name == 'nt':
        import ctypes
        drive=os.path.splitdrive(str(path))[0]
        if drive.startswith('\\\\') or (drive and ctypes.windll.kernel32.GetDriveTypeW(drive + '\\') == 4):
            raise ValueError('技能目录必须位于本机磁盘。')
    return path


def _skill_base(root=None, folder=''):
    # root is the .comfypilot environment root when injected by tests.
    if not isinstance(folder, str): raise ValueError('技能目录无效。')
    if folder.strip():
        candidate = _local_skill_path(Path(folder.strip()).expanduser())
        base = _local_skill_path(candidate.resolve())
        if not base.is_dir(): raise ValueError('指定的技能目录不存在或不是文件夹。')
        return base
    environment = (root or agent_root()).resolve()
    base = (environment / 'agents' / 'skills').resolve()
    if not base.is_relative_to(environment) or not base.is_dir():
        raise ValueError('默认 .comfypilot/agents/skills 不存在或越界；请指定技能目录。')
    return _local_skill_path(base)


def _read_bounded(path, limit):
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('技能内容超过 128 KiB 限制。')
    return data.decode('utf-8')


def skill_catalog(root=None, folder=''):
    base = _skill_base(root, folder)
    candidates = {}
    catalog_bytes = 0
    index_bytes = 0
    for entry in base.iterdir():
        index_bytes += len(entry.name.encode('utf8')) + 64
        if index_bytes > 8 * 1024 * 1024: raise OverflowError('技能目录索引超过 8 MiB。')
        try:
            directory = entry.resolve()
            if directory.parent != base or not directory.is_dir():
                continue
            path = (directory / 'SKILL.md').resolve()
            if path.parent != directory or not path.is_file():
                continue
            text = _read_bounded(path, MAX_SKILL_BYTES)
            catalog_bytes += len(text.encode('utf8'))
            if catalog_bytes > 8 * 1024 * 1024:
                raise OverflowError('技能目录清单总内容超过 8 MiB。')
            lines = text.splitlines()
            if not lines or lines[0].strip() != '---':
                continue
            end = next(i for i in range(1, len(lines)) if lines[i].strip() == '---')
            manifest = yaml.load('\n'.join(lines[1:end]), Loader=_UniqueSafeLoader)
            if not isinstance(manifest, dict):
                continue
            name, description = manifest.get('name'), manifest.get('description')
            if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', name)
                or not isinstance(description, str) or not description.strip()
                or len(description) > 1024):
                continue
            candidates.setdefault(name.casefold(), []).append((name, description.strip(), directory))
        except (OSError, ValueError, UnicodeError, yaml.YAMLError, StopIteration, RecursionError, TypeError):
            continue
    valid = [items[0] for items in candidates.values() if len(items) == 1]
    return {name: (description, directory) for name, description, directory in sorted(valid, key=lambda item: item[0].casefold())}


def skill_text(name, resources='', root=None, catalog=None, folder=''):
    if not name or name == '(none)':
        if resources.strip():
            raise ValueError('技能资源需要先指定技能名称。')
        return ''
    if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
        raise ValueError('技能名称只能包含字母、数字、下划线和连字符。')
    entry = (catalog if catalog is not None else skill_catalog(root, folder)).get(name)
    if entry is None:
        raise ValueError('指定技能不存在、清单无效或存在重名。')
    skill = entry[1]
    result = []
    total = 0
    for rel in ['SKILL.md'] + [x.strip() for x in resources.splitlines() if x.strip()]:
        p = (skill / rel).resolve()
        if not p.is_relative_to(skill) or not p.is_file() or p.suffix.lower() not in ('.md', '.txt'):
            raise ValueError('技能资源必须是技能目录内的 Markdown 或文本文件。')
        content = _read_bounded(p, MAX_SKILL_BYTES - total)
        total += len(content.encode('utf-8'))
        result.append('--- ' + rel + ' ---\n' + content)
    return '\n\n'.join(result)

def check_interrupt():
    try:
        import comfy.model_management as mm
    except ImportError:
        return
    mm.throw_exception_if_processing_interrupted()

def encode_images(images, cancelled=None):
    import numpy as np
    from PIL import Image
    max_total = 5 * 1024 * 1024  # Base64 characters, not source-file bytes.
    source_pixels = 16 * 1024 * 1024
    count = 0
    for image in images:
        if len(image.shape) != 4 or image.shape[0] < 1 or image.shape[-1] not in (1, 3, 4):
            raise ValueError('IMAGE 必须是非空的单通道、RGB 或 RGBA 图像批次。')
        count += int(image.shape[0])
        if image.shape[1] < 1 or image.shape[2] < 1 or image.shape[1] * image.shape[2] > source_pixels:
            raise ValueError('单张图像尺寸无效或超过 16 兆像素，请先缩小图片。')
    if count > 8: raise ValueError('单次请求最多允许 8 张图像。')
    if not count: return []
    per_image = min(2 * 1024 * 1024, max_total // count)
    def checkpoint():
        if cancelled is not None and cancelled.is_set(): raise InterruptedError('图像编码已取消。')
        check_interrupt()
    blocks=[]
    for image in images:
        for frame in image:
            checkpoint()
            arr=np.array(frame.detach().cpu().numpy(),dtype=np.float32,copy=True)
            if not np.isfinite(arr).all(): raise ValueError('IMAGE 包含非有限像素值。')
            np.clip(arr,0,1,out=arr); arr *= 255
            arr=arr.astype(np.uint8)
            if arr.shape[-1]==1: arr=arr[:,:,0]
            source=Image.fromarray(arr)
            if source.mode == 'RGBA':
                rgb=Image.new('RGB',source.size,'white'); rgb.paste(source,mask=source.getchannel('A'))
            else: rgb=source.convert('RGB')
            initial=None; final=0; data=None
            for edge in (1280,1024,768):
                scaled=rgb.copy()
                scaled.thumbnail((edge,edge),Image.Resampling.LANCZOS)
                for quality in (85,70,60):
                    checkpoint(); buffer=io.BytesIO()
                    scaled.save(buffer,format='JPEG',quality=quality,optimize=False,subsampling=0)
                    candidate=buffer.getvalue(); final=((len(candidate)+2)//3)*4
                    if initial is None: initial=final
                    if final <= per_image:
                        data=candidate; break
                if data is not None: break
            if data is None:
                raise ValueError(f'图像 {rgb.width}×{rgb.height} 编码未能满足预算：初次 {initial} 字节，最终 {final} 字节，单张预算 {per_image} 字节（Base64）；请减少图片数量或缩小图片。')
            checkpoint()
            encoded=base64.b64encode(data).decode('ascii')
            blocks.append({'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+encoded}})
    return blocks

async def generate(prompt, system, context, base_url, model, key_env, temperature, max_tokens, timeout, skill='', resources='', root=None, *, profile_key=None, image=None, vision=False, include_raw=False):
    try:
        parts = urlsplit(base_url.strip())
        valid_url = parts.scheme in ('http', 'https') and parts.hostname and not (parts.username or parts.password or parts.query or parts.fragment)
        _ = parts.port
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError('base_url 必须是无凭据、查询参数和片段的有效 HTTP(S) 地址，例如 https://example.com/v1。') from None
    if not model.strip():
        raise ValueError('请填写模型标识 model。')
    if key_env and not re.fullmatch(r'GX_LLM_[A-Z0-9_]+_KEY', key_env):
        raise ValueError('api_key_env 必须是 GX_LLM_<名称>_KEY 环境变量名。')
    key = profile_key if profile_key is not None else (os.environ.get(key_env, '') if key_env else '')
    if profile_key is None and key_env and not key:
        raise ValueError('指定的 API Key 环境变量未设置。')
    if profile_key is None and key_env:
        allowed_url = os.environ.get(key_env[:-4] + '_BASE_URL', '').strip().rstrip('/')
        if not allowed_url or allowed_url != base_url.strip().rstrip('/'):
            raise ValueError('凭据环境变量对应的 BASE_URL 未设置或与节点地址不匹配。')
    selected = skill_text(skill, resources, root)
    messages = []
    if system.strip() or selected:
        messages.append({'role':'system', 'content': '\n\n'.join(x for x in (system, selected) if x)})
    user_text = '\n\n'.join(x for x in (context, prompt) if x)
    if image is not None:
        if not vision: raise ValueError('所选模型的视觉输入未启用；请检查 API Config 多模态模式及能力。')
        user_content = [{'type':'text', 'text':user_text}, *encode_images(image if isinstance(image, list) else [image])]
    else:
        user_content = user_text
    messages.append({'role':'user', 'content': user_content})
    body = json.dumps({'model':model, 'messages':messages, 'temperature':temperature, 'max_tokens':max_tokens}, ensure_ascii=False).encode('utf-8')
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError('请求内容超过 6 MiB 限制。')
    headers = {'Content-Type':'application/json', 'Accept':'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    async def fetch():
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False) as session:
            async with session.post(base_url.rstrip('/') + '/chat/completions', data=body, headers=headers, allow_redirects=False) as response:
                if response.status != 200:
                    raise RuntimeError(f'模型服务返回 HTTP {response.status}；请检查地址、模型、认证及图像输入支持。')
                chunks = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    chunks.extend(chunk)
                    if len(chunks) > MAX_BYTES:
                        raise RuntimeError('模型响应超过 1 MiB 限制。')
                return bytes(chunks)

    async def run():
        task = asyncio.create_task(fetch())
        try:
            while not task.done():
                check_interrupt()
                await asyncio.wait({task}, timeout=0.2)
            return await task
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    check_interrupt()
    try:
        raw = await run()
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
        raise RuntimeError('模型服务连接失败或超时；请检查地址和网络。') from None
    check_interrupt()
    if len(raw) > MAX_BYTES:
        raise RuntimeError('模型响应超过 1 MiB 限制。')
    try:
        data = json.loads(raw)
        message = data['choices'][0]['message']
        if not isinstance(message, dict):
            raise ValueError()
        if message.get('tool_calls'):
            raise RuntimeError('模型返回工具调用，但当前请求为纯文本模式。')
        content = message['content']
        if not isinstance(content, str):
            raise ValueError()
    except (ValueError, KeyError, IndexError, TypeError):
        raise RuntimeError('模型响应格式无效：需要标准 Chat Completions 文本响应。') from None
    if key:
        content = content.replace(key, '[REDACTED]')
    if include_raw:
        choice = data['choices'][0]
        safe = {}
        finish = choice.get('finish_reason')
        if finish in ('stop', 'length', 'content_filter', 'tool_calls', 'function_call'): safe['finish_reason'] = finish
        usage = data.get('usage')
        if isinstance(usage, dict):
            safe['usage'] = {k: v for k, v in usage.items() if k in ('prompt_tokens', 'completion_tokens', 'total_tokens') and isinstance(v, int) and not isinstance(v, bool)}
        return content, json.dumps(safe, ensure_ascii=False)
    return content
