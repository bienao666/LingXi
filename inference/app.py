"""CPU-only, serial, fixed-artifact inference. No prompts or secrets in logs.

Only this allowlisted pair is loadable. Long inputs are processed in full in
bounded chunks; no tokenizer truncation silently discards a document tail.
"""
import base64
from contextlib import asynccontextmanager
import errno
import math
import os
import secrets
import struct
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
import requests

from builtin_models import BUILTIN_MODELS

try:
    import resource
except ImportError:
    resource = None  # Windows unit tests; the production container is Linux.

TOKEN = os.environ.get('LINGXI_MEMORY_GATEWAY_TOKEN', '')
CORE = os.environ.get('LINGXI_CORE_URL', 'http://core:8000').rstrip('/')
lock = threading.Lock()
preparation = threading.Lock()
states = {p: {'state': 'not_downloaded', 'notice': '等待准备模型', 'model': s['name'], 'revision': s['revision']}
    for p, s in BUILTIN_MODELS.items()}
loaded = {}


def failure_notice(error, stage):
    """Expose a useful cause without upstream URLs, bodies or credentials."""
    response = getattr(error, 'response', None)
    if response is not None and isinstance(getattr(response, 'status_code', None), int):
        reason = '上游 HTTP '+str(response.status_code)
    elif isinstance(error, MemoryError) or isinstance(error, OSError) and error.errno == errno.ENOMEM:
        reason = '内存不足'
    elif isinstance(error, OSError) and error.errno == errno.ENOSPC:
        reason = '磁盘空间不足'
    elif isinstance(error, requests.Timeout):
        reason = '连接超时'
    elif isinstance(error, requests.ConnectionError):
        reason = '网络或代理连接失败'
    else:
        reason = type(error).__name__
    return f'{stage}失败（{reason}）；请核对代理、资源或模型缓存后重试'


def identity(request: Request):
    if not TOKEN or not secrets.compare_digest(request.headers.get('authorization', ''), 'Bearer '+TOKEN):
        raise HTTPException(401, '无效的内部服务身份')


def core_configuration():
    with requests.Session() as session:
        session.trust_env = False
        result = session.get(CORE+'/internal/memory-inference/config',
            headers={'Authorization': 'Bearer '+TOKEN}, timeout=10)
        result.raise_for_status()
        return result.json()


def install_download_transport(config):
    from huggingface_hub import configure_http_backend
    class DownloadSession(requests.Session):
        def __init__(self):
            super().__init__(); self.trust_env = False

        def request(self, method, url, **kwargs):
            # Every redirect is prepared by requests using the same session
            # proxies. Never silently bypass a configured proxy on download.
            proxies = {}
            if config.get('enabled'):
                https = config.get('https_proxy') or config.get('all_proxy')
                http = config.get('http_proxy') or config.get('all_proxy')
                if not https or not http:
                    raise RuntimeError('代理未覆盖模型下载请求')
                proxies = {'https': https, 'http': http}
            if requests.utils.should_bypass_proxies(url, no_proxy=','.join(config.get('no_proxy', []))):
                proxies = {}
            kwargs['proxies'] = proxies
            kwargs.setdefault('timeout', 60)
            return super().request(method, url, **kwargs)

        def rebuild_proxies(self, prepared_request, proxies):
            if requests.utils.should_bypass_proxies(prepared_request.url, no_proxy=','.join(config.get('no_proxy', []))):
                return {}
            return dict(proxies)
    configure_http_backend(backend_factory=DownloadSession)


def prepare_models():
    if not preparation.acquire(blocking=False):
        return
    try:
        config = core_configuration()
        install_download_transport(config['proxy'])
        from huggingface_hub import snapshot_download
        import torch
        from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer
        torch.set_num_threads(2)
        # Loading and inference share one admission slot, avoiding simultaneous
        # high-memory allocations. A failed model doesn't claim a healthy state.
        with lock:
            for purpose, spec in BUILTIN_MODELS.items():
                if purpose in loaded:
                    continue
                try:
                    states[purpose].update(state='downloading', notice='正在下载固定版本模型，首次准备可能较久')
                    weight = 'model.safetensors' if purpose == 'rerank' else 'pytorch_model.bin'
                    patterns = ['config.json', 'tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json',
                        'sentencepiece.bpe.model', weight]
                    try:
                        directory = snapshot_download(spec['name'], revision=spec['revision'], local_files_only=True)
                        if not all(Path(directory, name).is_file() for name in ('config.json', 'tokenizer.json', weight)):
                            raise FileNotFoundError('Incomplete snapshot')
                    except Exception:
                        directory = snapshot_download(spec['name'], revision=spec['revision'], allow_patterns=patterns)
                    states[purpose].update(state='loading', notice='正在加载 CPU 模型')
                    tokenizer = AutoTokenizer.from_pretrained(directory, local_files_only=True, trust_remote_code=False)
                    cls = AutoModel if purpose == 'embedding' else AutoModelForSequenceClassification
                    model = cls.from_pretrained(directory, local_files_only=True, trust_remote_code=False,
                        use_safetensors=purpose == 'rerank', weights_only=True).to('cpu').eval()
                    loaded[purpose] = (tokenizer, model)
                    states[purpose].update(state='ready', notice='固定版本 CPU 模型已加载；中文质量待专项评测')
                except Exception as exc:
                    stage = '模型加载' if states[purpose]['state'] == 'loading' else '模型下载'
                    states[purpose].update(state='failed', notice=failure_notice(exc, stage))
    except Exception as exc:
        for purpose in states:
            if purpose not in loaded:
                states[purpose].update(state='failed', notice=failure_notice(exc, '核心配置或推理依赖准备'))
    finally:
        preparation.release()


def automatic_prepare():
    # Wait for the authoritative Core; startup never invokes an external LLM.
    for _ in range(30):
        try:
            if core_configuration()['mode'] == 'simple':
                prepare_models()
            return
        except Exception:
            time.sleep(2)


@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=automatic_prepare, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get('/health/live')
def live():
    return {'status': 'alive'}


@app.get('/status', dependencies=[Depends(identity)])
def status():
    return {'models': {p: dict(s) for p, s in states.items()},
        'peak_rss_mib': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024, 1) if resource else None, 'device': 'cpu', 'concurrency': 1}


@app.post('/prepare', dependencies=[Depends(identity)])
def prepare():
    threading.Thread(target=prepare_models, daemon=True).start()
    return {'accepted': True}


class EmbeddingInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str
    input: str | list[str]
    dimensions: int = 1024
    encoding_format: str = 'float'


class RerankInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str
    query: str = Field(min_length=1, max_length=12000)
    documents: list[str] = Field(min_length=1, max_length=100)
    top_n: int | None = Field(default=None, ge=1, le=100)
    return_documents: bool = False


def model_for(purpose, name):
    if name != BUILTIN_MODELS[purpose]['name']:
        raise HTTPException(422, '不支持的内置模型')
    if purpose not in loaded:
        raise HTTPException(503, states[purpose]['notice'])
    return loaded[purpose]


def admit():
    # Waiting indefinitely ties up the gateway/worker. The caller sees a real
    # retryable availability error; inference never runs concurrently.
    if not lock.acquire(timeout=1):
        raise HTTPException(503, '内置推理正忙，请稍后重试')


def text_ids(tokenizer, text):
    if not isinstance(text, str) or not text.strip() or len(text) > 20000:
        raise HTTPException(422, '每段内置推理输入须为 1–20000 字符的非空文字')
    ids = tokenizer.encode(text, add_special_tokens=False)
    if len(ids) > 16384:
        raise HTTPException(422, '内置推理输入超出完整处理预算，请缩小范围')
    return ids


@app.post('/embeddings', dependencies=[Depends(identity)])
def embeddings(body: EmbeddingInput):
    if body.dimensions != 1024 or body.encoding_format not in ('float', 'base64'):
        raise HTTPException(422, '内置嵌入仅支持 1024 维 float/base64，不补齐或截断向量')
    inputs = [body.input] if isinstance(body.input, str) else body.input
    if not 1 <= len(inputs) <= 100 or sum(len(s) for s in inputs) > 100000:
        raise HTTPException(422, '内置嵌入每批最多 100 段、合计 100000 字符')
    tokenizer, model = model_for('embedding', body.model)
    admit(); started = time.monotonic()
    try:
        import torch
        vectors, tokens = [], 0
        with torch.inference_mode():
            for value in inputs:
                ids = text_ids(tokenizer, value); tokens += len(ids)
                chunks, weights = [], []
                for start in range(0, len(ids), 1022):
                    part = ids[start:start+1022]
                    features = tokenizer.prepare_for_model(part, return_tensors='pt', return_token_type_ids=False, prepend_batch_axis=True)
                    vector = torch.nn.functional.normalize(model(**features).last_hidden_state[:, 0], p=2, dim=1)[0]
                    chunks.append(vector); weights.append(len(part))
                combined = sum(v*w for v, w in zip(chunks, weights))/sum(weights)
                vector = torch.nn.functional.normalize(combined, p=2, dim=0).tolist()
                if len(vector) != 1024 or not all(math.isfinite(v) for v in vector):
                    raise RuntimeError('Invalid embedding')
                vectors.append(vector)
        states['embedding'].update(state='ready', notice='固定版本 CPU 模型已加载；中文质量待专项评测')
        return {'object': 'list', 'model': body.model, 'data': [{'object': 'embedding', 'index': i,
            'embedding': base64.b64encode(struct.pack('<1024f', *v)).decode() if body.encoding_format == 'base64' else v}
            for i, v in enumerate(vectors)], 'usage': {'prompt_tokens': tokens, 'total_tokens': tokens},
            'local_metrics': {'duration_ms': int((time.monotonic()-started)*1000)}}
    except (MemoryError, RuntimeError) as exc:
        states['embedding'].update(state='failed', notice='嵌入推理失败，可能资源不足；文字聊天仍可使用')
        raise HTTPException(503, states['embedding']['notice']) from None
    finally:
        lock.release()


@app.post('/rerank', dependencies=[Depends(identity)])
def rerank(body: RerankInput):
    if sum(len(s) for s in body.documents) > 100000:
        raise HTTPException(422, '重排文档合计超出处理预算')
    tokenizer, model = model_for('rerank', body.model)
    query = text_ids(tokenizer, body.query)
    if len(query) > 512:
        raise HTTPException(422, '重排查询超过 512 token，请缩小范围')
    admit()
    try:
        import torch
        results = []
        with torch.inference_mode():
            for index, document in enumerate(body.documents):
                ids = text_ids(tokenizer, document); scores = []
                for start in range(0, len(ids), 512):
                    features = tokenizer.prepare_for_model(query, pair_ids=ids[start:start+512],
                        return_tensors='pt', return_token_type_ids=False, prepend_batch_axis=True)
                    score = torch.sigmoid(model(**features).logits.reshape(-1)[0]).item()
                    if not math.isfinite(score):
                        raise RuntimeError('Invalid score')
                    scores.append(score)
                results.append({'index': index, 'relevance_score': max(scores),
                    **({'document': {'text': document}} if body.return_documents else {})})
        results.sort(key=lambda row: (-row['relevance_score'], row['index']))
        states['rerank'].update(state='ready', notice='固定版本 CPU 模型已加载；中文质量待专项评测')
        return {'model': body.model, 'results': results[:body.top_n or len(results)]}
    except (MemoryError, RuntimeError):
        states['rerank'].update(state='failed', notice='重排推理失败，可能资源不足；文字聊天仍可使用')
        raise HTTPException(503, states['rerank']['notice']) from None
    finally:
        lock.release()
