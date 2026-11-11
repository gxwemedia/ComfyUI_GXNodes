"""Model request helpers for GX LLM. No model or credential values are logged."""
import asyncio
import json
import math
import threading
import re
from urllib.parse import urlsplit
import aiohttp
from .gxllm_backend import check_interrupt, encode_images, MAX_REQUEST_BYTES, MAX_BYTES

class _SafeError(RuntimeError):
    pass

_RESERVED_EXTRA = frozenset({'model','messages','stream','tools','tool_choice','functions','function_call','temperature','top_p','presence_penalty','frequency_penalty','seed','stop','response_format','reasoning_effort','max_tokens','max_completion_tokens','headers','extra_headers','base_url','image_url'})

def _safe_extra(value, depth=0):
    if depth > 8: return False
    if isinstance(value,dict):
        return all(isinstance(k,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',k) and k.lower() not in _RESERVED_EXTRA and not re.search(r'(?i)api.?key|secret|password|authorization|credential|access.?token',k) and _safe_extra(v,depth+1) for k,v in value.items())
    if isinstance(value,list): return len(value)<=64 and all(_safe_extra(v,depth+1) for v in value)
    if isinstance(value,float): return math.isfinite(value)
    return value is None or isinstance(value,(str,int,bool))

def generation_options(g):
    if not isinstance(g, dict): raise ValueError('生成参数配置无效。')
    allowed={'token_field','max_tokens','temperature','top_p','presence_penalty','frequency_penalty','seed','reasoning_effort','stop','response_format','advanced_extra_body'}
    if set(g)-allowed: raise ValueError('生成参数含不支持的字段：' + ', '.join(sorted(set(g)-allowed)))
    field=g.get('token_field','max_tokens'); limit=g.get('max_tokens',4096)
    if field not in ('max_tokens','max_completion_tokens') or not isinstance(limit,int) or not 1 <= limit <= 32768: raise ValueError('输出 token 参数无效。')
    temperature=g.get('temperature',0.7); top_p=g.get('top_p',1.0); presence=g.get('presence_penalty',0.0); frequency=g.get('frequency_penalty',0.0)
    if not (0 <= temperature <= 2 and 0 < top_p <= 1 and -2 <= presence <= 2 and -2 <= frequency <= 2): raise ValueError('采样参数超出范围。')
    result={field:limit}
    if 'temperature' in g: result['temperature']=temperature
    if 'top_p' in g: result['top_p']=top_p
    if 'presence_penalty' in g: result['presence_penalty']=presence
    if 'frequency_penalty' in g: result['frequency_penalty']=frequency
    seed=g.get('seed',-1)
    if not isinstance(seed,int) or seed < -1: raise ValueError('seed 无效。')
    if seed >= 0: result['seed']=seed
    reasoning=g.get('reasoning_effort','default')
    if reasoning not in ('default','none','minimal','low','medium','high','xhigh','max'): raise ValueError('reasoning_effort 无效。')
    if reasoning != 'default': result['reasoning_effort']=reasoning
    stop=g.get('stop','')
    if not isinstance(stop,str): raise ValueError('stop 无效。')
    stops=[s for s in stop.splitlines() if s]
    if len(stops)>4 or any(len(s)>256 for s in stops): raise ValueError('stop 最多四行，每行最多 256 字符。')
    if stops: result['stop']=stops
    fmt=g.get('response_format','default')
    if fmt not in ('default','text','json_object'): raise ValueError('response_format 无效。')
    if fmt != 'default': result['response_format']={'type':fmt}
    extra=g.get('advanced_extra_body','').strip()
    if len(extra)>4096: raise ValueError('扩展参数超过 4096 字符。')
    if extra:
        try: value=json.loads(extra)
        except (ValueError, RecursionError): raise ValueError('扩展参数必须是 JSON 对象。') from None
        if not isinstance(value,dict) or not _safe_extra(value): raise ValueError('扩展参数含受保护字段或结构无效。')
        result['extra_body']=value
    return result

def options_for_protocol(options, protocol):
    if protocol != 'openai-completions': raise ValueError('仅支持 OpenAI Chat Completions 兼容协议。')
    result=dict(options)
    if result.get('reasoning_effort') == 'max': raise ValueError('reasoning_effort=max 不受支持。')
    return result


def validate_url(base_url):
    try:
        p=urlsplit(base_url.strip()); valid=p.scheme in ('http','https') and p.hostname and not (p.username or p.password or p.query or p.fragment); _=p.port
    except (ValueError,AttributeError): valid=False
    if not valid: raise ValueError('API 地址必须是无凭据的有效 HTTP(S) 地址。') from None
    return base_url.strip().rstrip('/')

async def request_text(base_url, model, key, protocol, prompt, system, context, skill_context, images, vision, generation, timeout):
    url=validate_url(base_url)
    if not isinstance(model,str) or not model.strip(): raise ValueError('请配置模型标识。')
    if images and not vision: raise ValueError('所选模型的图像输入未启用或未声明。')
    options=options_for_protocol(generation_options(generation), protocol)
    texts='\n\n'.join(x for x in (context,prompt) if x)
    sys_text='\n\n'.join(x for x in (system,skill_context) if x)
    image_blocks=[]
    if images:
        cancelled=threading.Event()
        task=asyncio.create_task(asyncio.to_thread(encode_images,images,cancelled))
        def consume_result(future):
            try: future.result()
            except BaseException: pass
        try:
            image_blocks=await asyncio.wait_for(asyncio.shield(task),timeout=timeout)
        except asyncio.TimeoutError:
            cancelled.set();task.add_done_callback(consume_result)
            raise ValueError('图像编码超时；请减少图片数量或缩小图片。') from None
        except BaseException:
            cancelled.set();task.add_done_callback(consume_result)
            raise
    if len(json.dumps([texts,sys_text,image_blocks],ensure_ascii=False).encode()) > MAX_REQUEST_BYTES: raise ValueError('请求内容超过 6 MiB。')
    async def run_request():
        try:
            from openai import AsyncOpenAI, APITimeoutError, BadRequestError
            import httpx2
        except ImportError: raise RuntimeError('当前 Python 环境缺少 OpenAI SDK。') from None
        messages=[]
        if sys_text: messages.append({'role':'system','content':sys_text})
        messages.append({'role':'user','content':([{'type':'text','text':texts},*image_blocks] if image_blocks else texts)})
        if len(json.dumps({'model':model,'messages':messages,'stream':True,**options},ensure_ascii=False,separators=(',',':')).encode('utf8')) > MAX_REQUEST_BYTES: raise ValueError('请求内容超过 6 MiB。')
        chunks=[]; size=0; finish=None; usage={}
        class LimitedStream(httpx2.AsyncByteStream):
            def __init__(self, stream): self.stream=stream; self.size=0
            async def __aiter__(self):
                async for chunk in self.stream:
                    self.size += len(chunk)
                    if self.size > MAX_BYTES: raise _SafeError('模型响应超过 1 MiB。')
                    yield chunk
            async def aclose(self): await self.stream.aclose()
        class LimitedTransport(httpx2.AsyncBaseTransport):
            def __init__(self): self.inner=httpx2.AsyncHTTPTransport()
            async def handle_async_request(self, request):
                if len(await request.aread()) > MAX_REQUEST_BYTES: raise _SafeError('请求内容超过 6 MiB。')
                if not key: request.headers.pop('authorization',None)
                response=await self.inner.handle_async_request(request)
                if response.headers.get('content-encoding','identity').lower() not in ('','identity'):
                    await response.aclose()
                    raise _SafeError('模型服务返回不支持的压缩响应。')
                return httpx2.Response(response.status_code,headers=response.headers,stream=LimitedStream(response.stream),request=request,extensions=response.extensions)
            async def aclose(self): await self.inner.aclose()
        try:
            async with httpx2.AsyncClient(trust_env=False,follow_redirects=False,transport=LimitedTransport(),headers={'accept-encoding':'identity'}) as transport:
                async with AsyncOpenAI(api_key=key or 'not-needed',base_url=url,http_client=transport,max_retries=0,timeout=timeout) as client:
                    stream=await client.chat.completions.create(model=model,messages=messages,stream=True,**options)
                    async for part in stream:
                        if part.choices:
                            choice=part.choices[0]
                            if choice.delta.tool_calls: raise _SafeError('模型返回了未启用的工具调用。')
                            text=choice.delta.content or ''
                            if not isinstance(text,str): raise _SafeError('模型文本响应格式无效。')
                            size += len(text.encode('utf8'))
                            if size > MAX_BYTES: raise _SafeError('模型响应超过 1 MiB。')
                            chunks.append(text)
                            if choice.finish_reason in ('tool_calls','function_call'): raise _SafeError('模型返回了未启用的工具调用。')
                            if choice.finish_reason: finish=choice.finish_reason
                        if part.usage:
                            usage={k:getattr(part.usage,k) for k in ('prompt_tokens','completion_tokens','total_tokens') if isinstance(getattr(part.usage,k,None),int)}
        except (_SafeError, asyncio.CancelledError): raise
        except (APITimeoutError, httpx2.TimeoutException, asyncio.TimeoutError): raise RuntimeError('模型请求超时。') from None
        except BadRequestError: raise RuntimeError('模型服务拒绝了请求；请检查所选模型是否支持图像输入及配置的生成参数。') from None
        except Exception: raise RuntimeError('模型服务请求失败；请检查服务地址、模型、认证及参数支持。') from None
        if finish is None: raise RuntimeError('模型响应未正常结束。')
        text=''.join(chunks); safe={'finish_reason':finish if finish in ('stop','length','content_filter') else 'other','usage':usage}
        if not text.strip():
            reason=safe['finish_reason']
            if reason in ('max_tokens','length'):
                raise RuntimeError('模型未返回可显示的文本，输出 token 已达到上限；可在 GX Generation Config 中提高上限或调整推理设置后再试。')
            if reason=='content_filter':
                raise RuntimeError('模型未返回可显示的文本，服务报告内容过滤。')
            raise RuntimeError('模型响应已结束，但没有返回可显示的文本；请检查模型与服务配置。')
        if key: text=text.replace(key,'[REDACTED]')
        return text,json.dumps(safe,ensure_ascii=False)
    check_interrupt()
    task=asyncio.create_task(run_request())
    try:
        async with asyncio.timeout(timeout):
            while not task.done():
                check_interrupt(); await asyncio.wait({task},timeout=0.2)
            return await task
    except asyncio.TimeoutError: raise RuntimeError('模型请求超时。') from None
    finally:
        if not task.done():
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
