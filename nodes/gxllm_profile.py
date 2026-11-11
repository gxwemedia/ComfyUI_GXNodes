"""Read-only access to the explicit local Harness web profile."""
import os
import re
from pathlib import Path
import yaml
from .gxllm_backend import agent_root

_LIMIT = 2 * 1024 * 1024

def _read(home, relative, missing=False):
    path = (home / relative).resolve()
    if not path.is_relative_to(home.resolve()):
        raise ValueError('DSH 配置路径超出 .comfypilot。')
    try:
        with path.open('rb') as stream:
            data = stream.read(_LIMIT + 1)
        if len(data) > _LIMIT: raise ValueError()
        return yaml.safe_load(data)
    except FileNotFoundError:
        if missing: return None
        raise ValueError('DSH 模型配置不存在。') from None
    except (OSError, ValueError, TypeError, yaml.YAMLError, RecursionError):
        raise ValueError('DSH 配置无效或不可读取。') from None

def _entries(root=None):
    pilot = (root or agent_root()).resolve()
    home = (pilot / 'dsh').resolve()
    if not home.is_relative_to(pilot): raise ValueError('DSH 目录位于 .comfypilot 之外。')
    data = _read(home, Path('profiles/web/cordis.patch.yml'))
    if not isinstance(data, list): raise ValueError('DSH Web profile 格式无效。')
    selected = [x for x in data if isinstance(x, dict) and x.get('id') == 'agent-default-model' and x.get('name') == '@deepseek-ai/dsh-agent-default-model']
    profiles = [x for x in data if isinstance(x, dict) and x.get('id') == 'llm-pi-ai' and x.get('name') == '@deepseek-ai/dsh-llm-pi-ai']
    if len(selected) != 1 or len(profiles) > 1: raise ValueError('DSH Web profile 缺少唯一模型配置。')
    default = selected[0].get('config'); config = profiles[0].get('config') if profiles else {'providers':{}}
    if not isinstance(default, dict) or not isinstance(config, dict) or not isinstance(config.get('providers'), dict): raise ValueError('DSH 模型配置格式无效。')
    return home, default, config['providers']

def profile_options(root=None):
    _, _, providers = _entries(root)
    choices = []
    for provider, p in providers.items():
        if not isinstance(provider, str) or '::' in provider or not isinstance(p, dict) or p.get('api') != 'openai-completions': continue
        models = p.get('models', [])
        if not isinstance(models, list): continue
        for m in models:
            if isinstance(m, dict) and isinstance(m.get('id'), str) and m['id'] and '::' not in m['id']:
                choices.append(provider + '::' + m['id'])
    return sorted(set(choices))

def resolve_profile(selection, root=None):
    if selection == '(select model)':
        raise ValueError('请在 API 配置中明确选择一个兼容模型；当前 DSH 默认模型不支持此节点的 Chat Completions 协议。')
    home, default, providers = _entries(root)
    if selection == '(default)': provider, model = default.get('provider'), default.get('model')
    else:
        parts = selection.split('::')
        if len(parts) != 2: raise ValueError('请选择有效的 DSH provider 和模型。')
        provider, model = parts
    if not isinstance(provider, str) or not isinstance(model, str): raise ValueError('DSH 默认模型配置无效。')
    p = providers.get(provider)
    if not isinstance(p, dict) or p.get('api') != 'openai-completions': raise ValueError('所选 DSH 模型不是已声明的 OpenAI Chat Completions 配置；请选择兼容模型或在 GX API Config 的 custom 中填写兼容地址和模型。')
    if not isinstance(model, str) or not model or not isinstance(p.get('baseURL'), str): raise ValueError('DSH 模型或地址配置无效。')
    if not isinstance(p.get('models'), list): raise ValueError('DSH 模型列表无效。')
    models = [m for m in p['models'] if isinstance(m, dict) and m.get('id') == model]
    if len(models) != 1: raise ValueError('所选 DSH 模型不存在或重复。')
    ref = p.get('apiKeyEnv', '')
    if not isinstance(ref, str) or (ref and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', ref)): raise ValueError('DSH 凭据引用无效。')
    inputs = models[0].get('input', p.get('defaultInput', []))
    vision = isinstance(inputs, list) and 'image' in inputs
    return {'base_url': p.get('baseURL', ''), 'model': model, 'key_ref': ref, 'vision': vision, 'home': home, 'protocol':'openai-completions'}

def resolve_key(profile):
    ref = profile['key_ref']
    if not ref: return ''
    document = _read(profile['home'], Path('.credentials.yaml'), missing=True)
    if document not in (None, {}):
        if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('refs', {}), dict): raise ValueError('DSH 凭据文件格式不受支持。')
        value = document.get('refs', {}).get(ref)
        if isinstance(value, str) and value: return value
    value = os.environ.get(ref, '')
    if not value: raise ValueError('DSH 凭据引用在现有配置和进程环境中不可用。')
    return value
