import hashlib
import asyncio
import threading
import re
import os
import json
from comfy_api.latest import io
from .gxllm_backend import skill_catalog, skill_text, _skill_base
from .gxllm_runtime import generation_options, options_for_protocol, request_text
from .gxllm_profile import profile_options, resolve_profile, resolve_key


def _skill_options():
    try:
        return ['(none)', *skill_catalog()]
    except (OSError, ValueError):
        return ['(none)']


def _profile_options():
    try:
        choices = profile_options()
        try:
            resolve_profile('(default)')
            return ['(default)', *choices]
        except (OSError, ValueError):
            return ['(select model)', *choices]
    except (OSError, ValueError):
        return ['(select model)']


class GXAPIConfig(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        profiles = _profile_options()
        return io.Schema(node_id='GXAPIConfig', display_name='GX API Config', category='GXNodes/LLM', inputs=[
            io.DynamicCombo.Input('source', options=[
                io.DynamicCombo.Option('dsh', [io.Combo.Input('dsh_model', profiles, default=profiles[0], display_name='模型（本地配置）')]),
                io.DynamicCombo.Option('custom', [io.String.Input('base_url', default='http://127.0.0.1:1234/v1', display_name='API 地址'), io.String.Input('model', default='', display_name='模型名称'), io.String.Input('api_key_env', default='', display_name='API Key 环境变量')]),
            ], display_name='配置来源'),
            io.Combo.Input('vision', ['auto','disabled','enabled'], default='auto', display_name='视觉输入'),
            io.Int.Input('timeout_seconds', default=60, min=1, max=600, display_name='请求超时（秒）'),
        ], outputs=[io.Custom('GX_API_CONFIG').Output('api_config', display_name='API 配置')])

    @classmethod
    def fingerprint_inputs(cls, source, vision, timeout_seconds):
        if not isinstance(source, dict) or source.get('source') != 'dsh': return 'custom'
        try:
            p=resolve_profile(source.get('dsh_model','(default)'))
            data=[p['base_url'],p['model'],p['vision'],p['key_ref'],p['protocol'],p.get('thinking'),p.get('reasoning_effort')]
            return hashlib.sha256(json.dumps(data,ensure_ascii=False).encode('utf8')).hexdigest()
        except (ValueError,OSError): return 'unavailable'

    @classmethod
    def execute(cls, source, vision, timeout_seconds):
        if not isinstance(source,dict) or source.get('source') not in ('dsh','custom'): raise ValueError('请选择有效的 API 配置来源。')
        return io.NodeOutput({'source':dict(source),'vision':vision,'timeout_seconds':timeout_seconds})


def _config_identity(api_config, profile=None):
    if not isinstance(api_config,dict) or not isinstance(api_config.get('source'),dict): raise ValueError('请连接有效的 GX API Config。')
    source=api_config['source']
    if source.get('source')=='dsh':
        profile=profile or resolve_profile(source.get('dsh_model','(default)'))
        protocol=profile['protocol']
        data=[protocol,profile['base_url'],profile['model'],profile['key_ref'],profile['vision'],profile.get('thinking'),profile.get('reasoning_effort')]
    elif source.get('source')=='custom':
        protocol='openai-completions'
        data=[protocol,source.get('base_url'),source.get('model'),source.get('api_key_env')]
    else: raise ValueError('API 配置来源无效。')
    data.extend([api_config.get('vision'),api_config.get('timeout_seconds')])
    try: fingerprint=hashlib.sha256(json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf8')).hexdigest()
    except (ValueError,TypeError): raise ValueError('API 配置无效。') from None
    return protocol,fingerprint


class GXGenerationConfig(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id='GXGenerationConfig', display_name='GX Generation Config', category='GXNodes/LLM', description='连接同一 API Config，并选择对应协议的生成设置。', inputs=[
            io.Custom('GX_API_CONFIG').Input('api_config', display_name='API 配置'),
            io.DynamicCombo.Input('settings', options=[
                io.DynamicCombo.Option('default', []),
                io.DynamicCombo.Option('openai-completions', [
                    io.Int.Input('max_tokens',default=4096,min=1,max=32768, display_name='最大输出 token'),
                    io.Float.Input('temperature',default=0.7,min=0.0,max=2.0,step=0.1, display_name='随机程度'),
                    io.Float.Input('top_p',default=1.0,min=0.01,max=1.0,step=0.01, display_name='采样范围（top_p）'),
                    io.Combo.Input('reasoning_effort',['default','none','minimal','low','medium','high','xhigh'],default='default', display_name='推理强度'),
                    io.Combo.Input('token_field',['max_tokens','max_completion_tokens'],default='max_tokens', display_name='输出上限字段', advanced=True),
                    io.Float.Input('presence_penalty',default=0.0,min=-2.0,max=2.0,step=0.1, display_name='话题重复惩罚', advanced=True),
                    io.Float.Input('frequency_penalty',default=0.0,min=-2.0,max=2.0,step=0.1, display_name='词频重复惩罚', advanced=True),
                    io.Combo.Input('api_seed_mode', ['off','use_execution_seed'], default='off', tooltip='仅支持该参数的 OpenAI 兼容服务：选择 use_execution_seed 才发送主节点的执行 seed。', display_name='发送模型种子', advanced=True),
                    io.String.Input('stop',multiline=True,default='', display_name='停止词（每行一个）', advanced=True),
                    io.Combo.Input('response_format',['default','text','json_object'],default='default', display_name='响应格式', advanced=True),
                    io.String.Input('advanced_extra_body',multiline=True,default='', display_name='扩展请求参数（JSON）', advanced=True),
                ]),
            ], display_name='生成预设'),
        ], outputs=[io.Custom('GX_REQUEST_CONFIG').Output('request_config', display_name='请求配置')])

    @classmethod
    def execute(cls, api_config, settings):
        protocol,binding=_config_identity(api_config)
        if not isinstance(settings,dict): raise ValueError('生成设置无效。')
        branch=settings.get('settings')
        if branch not in ('default','openai-completions'): raise ValueError('生成设置协议无效。')
        if branch not in ('default',protocol): raise ValueError('生成设置与 API Config 所选模型协议不一致；请切换生成设置分支。')
        values={k:v for k,v in settings.items() if k!='settings'}
        if branch == 'default' and values: raise ValueError('默认生成设置不接受额外字段。')
        if 'seed' in values: raise ValueError('API seed 必须通过 api_seed_mode 使用主节点执行 seed。')
        seed_mode=values.pop('api_seed_mode','off')
        if seed_mode not in ('off','use_execution_seed') or (protocol!='openai-completions' and seed_mode!='off'): raise ValueError('API seed 模式不适用于当前协议。')
        options_for_protocol(generation_options(values),protocol)
        return io.NodeOutput({'api_config':api_config,'protocol':protocol,'binding':binding,'values':values,'api_seed_mode':seed_mode})



def _skill_context(folder, skill_list):
    if not isinstance(skill_list,str): raise ValueError('技能列表无效。')
    rows=[line.strip() for line in skill_list.splitlines() if line.strip()]
    if '!directory-change' in rows: raise ValueError('技能目录已更改，请在技能配置中确认原有选择。')
    if not rows: return {'text':'','names':[]}
    catalog=skill_catalog(folder=folder)
    names=[]; parts=[]
    for row in rows:
        fields=[field.strip() for field in row.split('|')]
        name=fields[0]
        if not name or any(not x for x in fields[1:]): raise ValueError('技能列表每行格式为：技能名 | 可选资源路径。')
        if name.casefold() in {x.casefold() for x in names}: raise ValueError('不能重复选择同一技能。')
        names.append(name)
        parts.append(skill_text(name,'\n'.join(fields[1:]),catalog=catalog))
        if sum(len(x.encode('utf8')) for x in parts)>128*1024: raise ValueError('技能总文本超过 128 KiB。')
    text='\n\n'.join(parts)
    if len(text.encode('utf8'))>128*1024: raise ValueError('技能总文本超过 128 KiB。')
    return {'text':text,'names':names}


class GXSkillConfig(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id='GXSkillConfig', display_name='GX Skill Config', category='GXNodes/LLM', description='每行填写一个技能名，可用 | 添加资源路径；可增删或调整顺序。', inputs=[
            io.String.Input('skill_folder',default='',socketless=True,tooltip='留空使用当前环境 .comfypilot/agents/skills；填写路径则优先使用指定目录。', display_name='技能目录'),
            io.String.Input('skills',multiline=True,default='',socketless=True,tooltip='每行：技能名 | 可选资源1 | 可选资源2。用 GX Skill Catalog 预览可用名称。', display_name='已选技能'),
        ], outputs=[io.Custom('GX_SKILL_CONTEXT').Output('skill_context', display_name='技能上下文')])

    @classmethod
    def fingerprint_inputs(cls, skill_folder, skills):
        try:
            result=_skill_context(skill_folder,skills)
            return hashlib.sha256(json.dumps(result,ensure_ascii=False).encode('utf8')).hexdigest()
        except (ValueError,OSError,UnicodeError,OverflowError): return 'unavailable'

    @classmethod
    def execute(cls, skill_folder, skills):
        return io.NodeOutput(_skill_context(skill_folder,skills))


class GXLLMChatCompletions(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id='GXLLMChatCompletions', display_name='GX LLM Chat Completions', category='GXNodes/LLM', description='文本、技能上下文及最多八张图片；请连接 GX Generation Config。', inputs=[
            io.Custom('GX_REQUEST_CONFIG').Input('request_config', display_name='请求配置'),
            io.String.Input('prompt',multiline=True,default='', display_name='提示词'),
            io.String.Input('system',multiline=True,default='You are a helpful assistant.', display_name='角色指令'),
            io.String.Input('context',multiline=True,default='', display_name='补充上下文'),
            io.Int.Input('execution_seed',default=0,min=0,max=2147483647,control_after_generate=io.ControlAfterGenerate.fixed,tooltip='缓存/重跑标记；increment 自动更新并重新请求，fixed 可复用缓存。只有 API seed 开关启用时才发送给模型。', display_name='执行种子'),
            io.Custom('GX_SKILL_CONTEXT').Input('skill_context',optional=True, display_name='技能上下文'),
            *[io.Image.Input(f'image{i}',optional=True,display_name=f'图片 {i}') for i in range(1,9)],
            io.Video.Input('video',optional=True, display_name='视频'),
        ], outputs=[io.String.Output('response', display_name='回答文本'),io.String.Output('metadata', display_name='请求信息')])

    @classmethod
    async def execute(cls, system, prompt, context, execution_seed, request_config=None, skill_context=None, video=None, **kwargs):
        if set(kwargs) - {f'image{i}' for i in range(1,9)}: raise ValueError('GX LLM 收到不支持的输入，请连接 GX Generation Config。')
        if not isinstance(request_config,dict): raise ValueError('请将 GX Generation Config 的 request_config 连接到 GX LLM。')
        api_config=request_config.get('api_config')
        if not isinstance(api_config,dict) or not isinstance(api_config.get('source'),dict): raise ValueError('请连接有效的 GX API Config。')
        source=api_config['source']; mode=api_config.get('vision'); timeout=api_config.get('timeout_seconds')
        if mode not in ('auto','enabled','disabled') or not isinstance(timeout,int) or not 1 <= timeout <= 600: raise ValueError('API 配置无效。')
        if not isinstance(execution_seed,int) or isinstance(execution_seed,bool) or not 0 <= execution_seed <= 2147483647: raise ValueError('执行 seed 无效。')
        images=[kwargs[k] for k in (f'image{i}' for i in range(1,9)) if kwargs.get(k) is not None]
        if source.get('source')=='dsh':
            profile=resolve_profile(source.get('dsh_model','(default)'))
            base_url,model=profile['base_url'],profile['model']
            vision=mode=='enabled' or (mode=='auto' and profile['vision'])
            protocol=profile['protocol']
        elif source.get('source')=='custom':
            base_url,model,ref=source.get('base_url',''),source.get('model',''),source.get('api_key_env','')
            if ref and not re.fullmatch(r'GX_LLM_[A-Z0-9_]+_KEY',ref): raise ValueError('api_key_env 必须是 GX_LLM_<名称>_KEY 环境变量名。')
            vision=mode=='enabled'; protocol='openai-completions'
        else: raise ValueError('API 配置来源无效。')
        if (images or video is not None) and not vision: raise ValueError('所选模型的视觉能力未启用或未声明。')
        _,binding=_config_identity(api_config, profile if source.get('source')=='dsh' else None)
        if request_config.get('protocol') != protocol or request_config.get('binding') != binding: raise ValueError('模型配置已变化，请重新执行 GX Generation Config。')
        generation=dict(request_config.get('values',{}))
        if 'seed' in generation: raise ValueError('生成配置不能直接提供 seed；请使用 API seed 开关。')
        api_seed_mode=request_config.get('api_seed_mode','off')
        if api_seed_mode not in ('off','use_execution_seed'): raise ValueError('API seed 模式无效。')
        if api_seed_mode=='use_execution_seed':
            if protocol!='openai-completions': raise ValueError('当前模型协议不支持 API seed。')
            generation['seed']=execution_seed
        options=options_for_protocol(generation_options(generation),protocol)
        if not isinstance(skill_context,(dict,type(None))): raise ValueError('技能上下文无效。')
        skill_text_value=skill_context.get('text','') if skill_context else ''
        skill_names=skill_context.get('names',[]) if skill_context else []
        if not isinstance(skill_text_value,str) or len(skill_text_value.encode('utf8'))>128*1024 or not isinstance(skill_names,list) or any(not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',name) for name in skill_names): raise ValueError('技能上下文无效。')
        if video is not None:
            from .gxllm_video import sample_video
            cancelled=threading.Event()
            task=asyncio.create_task(asyncio.to_thread(sample_video,video,images,cancelled,with_times=True))
            def consume_result(future):
                try: future.result()
                except BaseException: pass
            try:
                frames,times=await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
                first_frame=sum(int(image.shape[0]) for image in images)+1
                images.extend(frames)
                context='\n\n'.join(x for x in (context,f'视频抽帧是第 {first_frame} 至 {first_frame+len(frames)-1} 张图；视频抽帧时间（秒，按视频帧顺序）：'+', '.join(f'{t:.3f}' for t in times)) if x)
            except asyncio.TimeoutError:
                cancelled.set(); task.add_done_callback(consume_result)
                raise ValueError('视频抽帧超时；底层解码将在检查点停止。') from None
            except BaseException:
                cancelled.set(); task.add_done_callback(consume_result)
                raise
        if source.get('source')=='dsh': key=resolve_key(profile)
        else:
            key=os.environ.get(ref,'') if ref else ''
            if ref and (not key or os.environ.get(ref[:-4]+'_BASE_URL','').strip().rstrip('/') != base_url.strip().rstrip('/')): raise ValueError('凭据引用未设置或与服务地址不匹配。')
        response,metadata=await request_text(base_url,model,key,protocol,prompt,system,context,skill_text_value,images,vision,generation,timeout)
        safe=json.loads(metadata); safe.update({'model':model.replace(key,'[REDACTED]') if key else model,'protocol':protocol,'image_count':sum(int(x.shape[0]) for x in images),'skills':[name.replace(key,'[REDACTED]') if key else name for name in skill_names]})
        return io.NodeOutput(response,json.dumps(safe,ensure_ascii=False))



class GXSkillCatalog(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id='GXSkillCatalog', display_name='GX Skill Catalog', category='GXNodes/LLM', description='列出本机技能名称与说明；目录变化时自动更新。', inputs=[io.String.Input('skill_folder', default='',socketless=True, display_name='技能目录')], outputs=[io.String.Output('STRING')])

    @classmethod
    def fingerprint_inputs(cls, skill_folder):
        try:
            catalog=skill_catalog(folder=skill_folder)
            listing=[(name, item[0], str(item[1])) for name,item in catalog.items()]
            return hashlib.sha256(json.dumps(listing,ensure_ascii=False).encode('utf8')).hexdigest()
        except (ValueError,OSError,UnicodeError,OverflowError): return 'unavailable'

    @classmethod
    def execute(cls, skill_folder):
        catalog = skill_catalog(folder=skill_folder)
        location = _skill_base(folder=skill_folder)
        listing = '\n\n'.join(f'{name}: {description}' for name, (description, _) in catalog.items()) or '目录存在，但没有有效技能。'
        return io.NodeOutput(f'技能目录：{location}\n\n{listing}')


V3_NODE_CLASSES = [GXAPIConfig, GXGenerationConfig, GXSkillConfig, GXLLMChatCompletions, GXSkillCatalog]


async def _skill_catalog_route(request):
    from aiohttp import web
    from .. import gxsecurity
    denied = gxsecurity.guard(request)
    if denied: return denied
    import secrets
    supplied=request.headers.get('X-GX-Token','')
    if request.remote not in ('127.0.0.1','::1','::ffff:127.0.0.1') or not secrets.compare_digest(supplied,gxsecurity.token()):
        return web.json_response({'error':'技能目录只能由本机已授权界面读取。'},status=403)
    if request.content_length is None or request.content_length > 8192:
        return web.json_response({'error':'技能目录请求无效。'},status=400)
    try:
        data=await request.json()
        folder=data.get('folder','') if isinstance(data,dict) else None
        if not isinstance(folder,str) or len(folder)>4096: raise ValueError('技能目录无效。')
        catalog=await asyncio.to_thread(skill_catalog,folder=folder)
        location=await asyncio.to_thread(_skill_base,folder=folder)
        return web.json_response({'skills':[{'name':name,'description':item[0]} for name,item in catalog.items()],'source':'custom' if folder.strip() else 'default','location':str(location)})
    except (OSError,ValueError,UnicodeError,OverflowError):
        return web.json_response({'error':'技能目录不可用或清单超过限制。'},status=400)


def register_routes():
    from server import PromptServer
    PromptServer.instance.routes.post('/gx/llm/skills')(_skill_catalog_route)
