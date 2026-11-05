"""Açıkta unutulmuş LLM sunucuları, her biri kendi portunda ve kendi parmak iziyle. Caddy portu önekle buraya yollar:
:8000 vLLM, :1234 LM Studio, :4000 LiteLLM proxy, :8080 llama.cpp server, :3000 Open WebUI (kayıt/giriş tuzağı),
:7860 Gradio (text-generation-webui). Sohbet uçlarının hepsi aynı ham moda bağlı (openai_compat.make_router).
Her ürün üç router: x (ürüne özgü uçlar ve make_router'ı ezenler) → api (OpenAI uyumlu) → tail (ürünün 404'ü)."""
import base64
import hashlib
import hmac
import json
import os
import random
import re
import string
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse

from ..config import settings
from ..detect.rules import LLM_KEY_MARK
from ..engine import agent, probes, tools
from . import anthropic_compat, ollama_compat
from .openai_compat import GinJSON, api_key, dumps, make_router, ntok, pieces

ROUTERS: list[APIRouter] = []  # main.py sırasıyla ekler
METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
NOT_FOUND = {"detail": "Not Found"}  # FastAPI tabanlılar: vLLM, LiteLLM, Open WebUI, Gradio
STARTED = time.time()


def _alnum(n: int, chars=string.ascii_letters + string.digits) -> str:
    return "".join(random.choices(chars, k=n))


def _tail(prefix: str, handler) -> APIRouter:
    t = APIRouter(prefix=prefix)
    t.add_api_route("/{path:path}", handler, methods=METHODS)
    return t


async def _json(request: Request) -> dict:
    try:
        p = await request.json()
    except Exception:
        return {}
    return p if isinstance(p, dict) else {}


def _swagger(title: str) -> HTMLResponse:
    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
<link type="text/css" rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
<link rel="shortcut icon" href="https://fastapi.tiangolo.com/img/favicon.png">
<title>{title}</title>
</head>
<body>
<div id="swagger-ui">
</div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>
const ui = SwaggerUIBundle({{url: '/openapi.json', "dom_id": "#swagger-ui", "layout": "BaseLayout",
  "deepLinking": true, "showExtensions": true, "showCommonExtensions": true,
  presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset]}})
</script>
</body>
</html>
""")


def _openapi(title: str, version: str, paths: list[str], desc: str = "") -> dict:
    info = {"title": title, "version": version}
    if desc:
        info["description"] = desc
    return {"openapi": "3.1.0", "info": info,
            "paths": {p: {("get" if p.startswith(("/health", "/version", "/v1/models", "/metrics", "/key/info",
                                                  "/model/info", "/load")) else "post"): {"summary": p.strip("/")}}
                      for p in paths}}


def _usage_tokens(o: dict) -> tuple[int, int]:
    u = o.get("usage") or {}
    return u.get("prompt_tokens", 0), u.get("completion_tokens", 0)


# ================================================================ vLLM (:8000)
VLLM = "/_vllm"
VLLM_MODEL = "meta-llama/Llama-3.3-70B-Instruct"
VLLM_VERSION = "0.10.1.1"
VLLM_CTX = 32768


def _vllm_card(m: str) -> dict:
    t = int(time.time())  # vLLM created'ı istek anında üretir
    return {"id": m, "object": "model", "created": t, "owned_by": "vllm", "root": m, "parent": None,
            "max_model_len": VLLM_CTX, "permission": [{
                "id": f"modelperm-{uuid.uuid4().hex}", "object": "model_permission", "created": t,
                "allow_create_engine": False, "allow_sampling": True, "allow_logprobs": True,
                "allow_search_indices": False, "allow_view": True, "allow_fine_tuning": False,
                "organization": "*", "group": None, "is_blocking": False}]}


def _vllm_shape(o: dict, kind: str) -> dict:
    if kind == "chat":
        o["choices"] = [{"index": c["index"], "message": {"role": "assistant", "reasoning_content": None,
                                                          "content": c["message"]["content"],
                                                          "tool_calls": c["message"].get("tool_calls", [])},
                         "logprobs": None, "finish_reason": c["finish_reason"], "stop_reason": None}
                        for c in o["choices"]]
    elif kind in ("chunk", "text_chunk", "text"):
        for c in o["choices"]:
            c.setdefault("logprobs", None)
            if c.get("finish_reason"):
                c["stop_reason"] = None
    if kind in ("chat", "text"):
        o["usage"]["prompt_tokens_details"] = None
        o.update(prompt_logprobs=None, kv_transfer_params=None)
    return o


def _vllm_err(msg: str, kind: str, code: int) -> JSONResponse:
    return JSONResponse({"error": {"message": msg, "type": kind, "param": None, "code": code}}, code)


vllm_x = APIRouter(prefix=VLLM)


@vllm_x.get("/health")
async def vllm_health():
    return Response(status_code=200)


@vllm_x.api_route("/ping", methods=["GET", "POST"])
async def vllm_ping():
    return Response(status_code=200)


@vllm_x.get("/version")
async def vllm_version():
    return {"version": VLLM_VERSION}


@vllm_x.get("/load")
async def vllm_load():
    return {"server_load": 0}


@vllm_x.get("/metrics")
async def vllm_metrics():
    up = time.time() - STARTED
    lab = f'engine="0",model_name="{VLLM_MODEL}"'
    rows = [("num_requests_running", "gauge", "Number of requests in model execution batches.", 0.0),
            ("num_requests_waiting", "gauge", "Number of requests waiting to be processed.", 0.0),
            ("kv_cache_usage_perc", "gauge", "KV-cache usage. 1 means 100 percent usage.", 0.0),
            ("prompt_tokens_total", "counter", "Number of prefill tokens processed.", float(int(up * 41.7))),
            ("generation_tokens_total", "counter", "Number of generation tokens processed.", float(int(up * 9.3)))]
    return PlainTextResponse("".join(f"# HELP vllm:{n} {h}\n# TYPE vllm:{n} {t}\nvllm:{n}{{{lab}}} {v}\n"
                                     for n, t, h, v in rows), media_type="text/plain; version=0.0.4; charset=utf-8")


@vllm_x.post("/tokenize")
async def vllm_tokenize(request: Request):
    p = await _json(request)
    text = str(p.get("prompt") or "") or "\n".join(str(m.get("content", "")) for m in p.get("messages") or []
                                                   if isinstance(m, dict))
    toks = [128000] + [1000 + (b * 7) % 120000 for b in text.encode()[: max(1, len(text) // 4)]]
    return {"count": len(toks), "max_model_len": VLLM_CTX, "tokens": toks, "token_strs": None}


@vllm_x.post("/v1/embeddings")
async def vllm_embeddings():
    return _vllm_err("The model does not support Embeddings API", "BadRequestError", 400)


@vllm_x.get("/openapi.json")
async def vllm_openapi():
    return _openapi("FastAPI", "0.1.0", ["/health", "/load", "/ping", "/tokenize", "/detokenize", "/v1/models",
                                         "/version", "/v1/responses", "/v1/chat/completions", "/v1/completions",
                                         "/v1/embeddings", "/pooling", "/classify", "/score", "/v1/score",
                                         "/v1/audio/transcriptions", "/rerank", "/v1/rerank", "/invocations",
                                         "/metrics"])


@vllm_x.get("/docs")
async def vllm_docs():
    return _swagger("FastAPI - Swagger UI")


async def vllm_404(path: str):
    return JSONResponse(NOT_FOUND, 404)


ROUTERS += [vllm_x, make_router(
    f"{VLLM}/v1", [VLLM_MODEL], owned_by="vllm", created=0, card=_vllm_card, shape=_vllm_shape,
    load=lambda m: m == VLLM_MODEL, new_id=lambda kind: f"{kind}-{uuid.uuid4().hex}",
    missing=lambda m: _vllm_err(f"The model `{m}` does not exist.", "NotFoundError", 404),
    busy={"error": {"message": "Service Unavailable", "type": "ServiceUnavailableError", "param": None, "code": 503}}),
    _tail(VLLM, vllm_404)]


# ================================================================ LM Studio (:1234, Express)
LMS = "/_lmstudio"
# (kimlik, mimari, quant, durum, bağlam, tür, yayıncı)
LMS_MODELS = [("qwen2.5-coder-32b-instruct", "qwen2", "Q4_K_M", "loaded", 32768, "llm", "lmstudio-community"),
              ("meta-llama-3.1-70b-instruct", "llama", "Q4_K_M", "loaded", 131072, "llm", "lmstudio-community"),
              ("deepseek-r1-distill-llama-70b", "llama", "Q4_K_M", "not-loaded", 131072, "llm", "lmstudio-community"),
              ("text-embedding-nomic-embed-text-v1.5", "nomic-bert", "Q8_0", "not-loaded", 2048, "embeddings",
               "nomic-ai")]
LMS_IDS = [m[0] for m in LMS_MODELS]


def _lms_shape(o: dict, kind: str) -> dict:
    if kind == "models":
        return {"data": o["data"], "object": "list"}
    if kind == "chat":
        o["choices"] = [{"index": c["index"], "logprobs": None, "finish_reason": c["finish_reason"],
                         "message": c["message"]} for c in o["choices"]]
        o.update(stats={}, system_fingerprint=o["model"])
    elif kind == "chunk":
        o = {k: v for k, v in o.items() if k != "choices"} | {"system_fingerprint": o["model"], "choices": [
            {"index": c["index"], "delta": c["delta"], "logprobs": None, "finish_reason": c["finish_reason"]}
            for c in o["choices"]]}
    return o


def _lms_missing(m: str) -> JSONResponse:
    return GinJSON({"error": {"message": f'Invalid model identifier "{m}". Please specify a valid downloaded model '
                              f'(i.e. {LMS_IDS[0]}).', "type": "invalid_request_error", "param": "model",
                              "code": "model_not_found"}}, 404)


lms_x = APIRouter(prefix=LMS)


@lms_x.get("/api/v0/models")
async def lms_v0_models(request: Request):
    request.state.tags.add("model_enum")
    return GinJSON({"object": "list", "data": [
        {"id": i, "object": "model", "type": kind, "publisher": pub, "arch": arch, "compatibility_type": "gguf",
         "quantization": q, "state": state, "max_context_length": ctx} for i, arch, q, state, ctx, kind, pub in LMS_MODELS]})


async def lms_404(path: str, request: Request):
    return GinJSON({"error": f"Unexpected endpoint or method. ({request.method} /{path})"}, 200)


ROUTERS += [lms_x, make_router(
    f"{LMS}/v1", LMS_IDS, owned_by="organization_owner", created=0, json_cls=GinJSON,
    card=lambda m: {"id": m, "object": "model", "owned_by": "organization_owner"}, shape=_lms_shape,
    load=lambda m: m in LMS_IDS, missing=_lms_missing, embed_dim=lambda m: 768,
    new_id=lambda kind: f"{kind}-{_alnum(21, string.ascii_lowercase + string.digits)}"),
    _tail(LMS, lms_404)]


# ================================================================ LiteLLM proxy (:4000)
LLT = "/_litellm"
LLT_VERSION = "1.77.7"
# (model_name, sağlayıcı, sağlayıcı modeli, mod)
LLT_MODELS = [("gpt-4o", "openai", "gpt-4o", "chat"), ("gpt-4o-mini", "openai", "gpt-4o-mini", "chat"),
              ("gpt-4.1", "openai", "gpt-4.1", "chat"), ("o3-mini", "openai", "o3-mini", "chat"),
              ("claude-sonnet-4-5", "anthropic", "claude-sonnet-4-5-20250929", "chat"),
              ("claude-opus-4-1", "anthropic", "claude-opus-4-1-20250805", "chat"),
              ("gemini-2.5-pro", "vertex_ai", "gemini-2.5-pro", "chat"),
              ("deepseek-chat", "deepseek", "deepseek-chat", "chat"),
              ("llama-3.3-70b", "together_ai", "meta-llama/Llama-3.3-70B-Instruct-Turbo", "chat"),
              ("text-embedding-3-small", "openai", "text-embedding-3-small", "embedding")]
LLT_IDS = [m[0] for m in LLT_MODELS]
LLT_KEYS = {"sk-1234"}  # dokümandaki varsayılan master key; /key/generate ile üretilenler eklenir


def _llt_err(msg: str, kind: str, code: int, param="None") -> JSONResponse:
    return JSONResponse({"error": {"message": msg, "type": kind, "param": param, "code": str(code)}}, code)


def _llt_gate(request: Request):
    k = api_key(request)
    if not k:
        return _llt_err("Authentication Error, No api key passed in.", "auth_error", 401)
    if k == "sk-1234":
        request.state.tags.add("default_credential")
    if k in LLT_KEYS or LLM_KEY_MARK in k:
        return None
    h = hashlib.sha256(k.encode()).hexdigest()
    return _llt_err(f"Authentication Error, Invalid proxy server token passed. Received API Key = {k[:5]}...{k[-4:]}, "
                    f"Key Hash (Token) ={h}. Unable to find token in cache or `LiteLLM_VerificationTokenTable`",
                    "auth_error", 401)


def _llt_headers():
    return {"x-litellm-call-id": str(uuid.uuid4()), "x-litellm-version": LLT_VERSION, "x-litellm-key-spend": "0.0",
            "x-litellm-response-duration-ms": f"{random.uniform(600, 2400):.3f}", "x-litellm-attempted-retries": "0"}


def _llt_shape(o: dict, kind: str) -> dict:
    if kind == "chat":
        pt, ct = _usage_tokens(o)
        return {"id": o["id"], "created": o["created"], "model": o["model"], "object": "chat.completion",
                "system_fingerprint": None, "choices": [
                    {"finish_reason": c["finish_reason"], "index": c["index"], "message": {
                        "content": c["message"]["content"], "role": "assistant",
                        "tool_calls": c["message"].get("tool_calls"), "function_call": None}} for c in o["choices"]],
                "usage": {"completion_tokens": ct, "prompt_tokens": pt, "total_tokens": pt + ct,
                          "completion_tokens_details": None, "prompt_tokens_details": None}}
    return o


def _llt_info(name: str, prov: str, real: str, mode: str) -> dict:
    return {"model_name": name, "litellm_params": {"model": f"{prov}/{real}", "use_in_pass_through": False,
                                                   "merge_reasoning_content_in_choices": False},
            "model_info": {"id": hashlib.sha256(f"llt|{name}".encode()).hexdigest(), "db_model": False, "key": real,
                           "mode": mode, "litellm_provider": prov, "max_tokens": 16384 if mode == "chat" else None,
                           "max_input_tokens": 128000 if mode == "chat" else 8191,
                           "supports_function_calling": mode == "chat", "supports_vision": prov != "deepseek"}}


llt_x = APIRouter(prefix=LLT)


@llt_x.get("/")
async def llt_root():
    return _swagger("LiteLLM API - Swagger UI")


@llt_x.get("/openapi.json")
async def llt_openapi():
    return _openapi("LiteLLM API", LLT_VERSION, ["/v1/models", "/v1/chat/completions", "/v1/completions",
                                                "/v1/embeddings", "/v1/messages", "/key/generate", "/key/info",
                                                "/model/info", "/health/liveliness", "/health/readiness"],
                    "Proxy Server to call 100+ LLMs in the OpenAI format. [**Customize Swagger Docs**]"
                    "(https://docs.litellm.ai/docs/proxy/enterprise#swagger-docs---custom-routes--branding)\n\n"
                    "👉 [```LiteLLM Admin Panel on /ui```](/ui/). Create, Edit Keys with SSO")


@llt_x.get("/health/liveliness")
@llt_x.get("/health/liveness")
async def llt_alive():
    return "I'm alive!"


@llt_x.get("/health/readiness")
async def llt_ready():
    return {"status": "healthy", "db": "connected", "cache": None, "litellm_version": LLT_VERSION,
            "success_callbacks": ["sync_deployment_callback_on_success", "_PROXY_VirtualKeyModelMaxBudgetLimiter",
                                  "_ProxyDBLogger", "ServiceLogging"], "use_aiohttp_transport": True,
            "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S.000000", time.gmtime())}


LLT_LOGIN = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>LiteLLM Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>body{font-family:system-ui,sans-serif;
background:#f8fafc;display:flex;justify-content:center;padding-top:12vh}form{background:#fff;padding:32px;
border-radius:8px;box-shadow:0 1px 3px #0002;width:340px}input{width:100%;padding:8px;margin:6px 0 14px;
box-sizing:border-box}button{width:100%;padding:9px;background:#4f46e5;color:#fff;border:0;border-radius:4px}</style>
</head><body><form method="post" action="/login"><h2>🚅 LiteLLM</h2><p>Login to the Admin UI</p>
<label>Username</label><input name="username" required><label>Password</label>
<input name="password" type="password" required><button>Submit</button></form></body></html>"""


@llt_x.get("/ui")
@llt_x.get("/ui/")
@llt_x.get("/sso/key/generate")
async def llt_ui():
    return HTMLResponse(LLT_LOGIN)


@llt_x.post("/login")
async def llt_login(request: Request):
    request.state.tags.add("credential_capture")
    try:
        form = await request.form()
    except Exception:
        form = {}
    if form.get("username") == "admin" and form.get("password") in LLT_KEYS:  # UI varsayılanı: admin / master key
        request.state.tags.add("default_credential")
        return Response(status_code=303, headers={"location": "/ui/?userID=default_user_id&login=success"})
    return _llt_err("Invalid credentials used to access UI.\nNot valid credentials for admin", "auth_error", 401,
                    "invalid_credentials")


@llt_x.get("/model/info")
@llt_x.get("/v1/model/info")
async def llt_model_info(request: Request):
    request.state.tags.add("model_enum")
    if deny := _llt_gate(request):
        return deny
    return {"data": [_llt_info(*m) for m in LLT_MODELS]}


@llt_x.get("/key/info")
async def llt_key_info(request: Request):
    if deny := _llt_gate(request):
        return deny
    k = api_key(request)
    return {"key": hashlib.sha256(k.encode()).hexdigest(), "info": {
        "key_name": f"sk-...{k[-4:]}", "key_alias": None, "spend": 0.0 if k == "sk-1234" else 18.42,
        "max_budget": None, "models": [], "user_id": "default_user_id", "team_id": None, "metadata": {},
        "expires": None, "tpm_limit": None, "rpm_limit": None, "created_at": "2026-09-14T08:21:37.514000Z"}}


@llt_x.post("/key/generate")
async def llt_key_generate(request: Request):
    if deny := _llt_gate(request):
        return deny
    request.state.tags.add("key_generate")
    p = await _json(request)
    k = f"sk-{_alnum(6)}{LLM_KEY_MARK}{_alnum(6)}"  # üretilen anahtar da işaretli: sonraki kullanım llm_key_reuse
    LLT_KEYS.add(k)
    return {"key": k, "key_name": f"sk-...{k[-4:]}", "expires": None, "models": p.get("models") or [],
            "spend": 0.0, "max_budget": p.get("max_budget"), "user_id": p.get("user_id") or "default_user_id",
            "team_id": p.get("team_id"), "metadata": p.get("metadata") or {},
            "token": hashlib.sha256(k.encode()).hexdigest(), "created_by": "default_user_id"}


async def llt_404(path: str):
    return JSONResponse(NOT_FOUND, 404)


_llt_api = dict(owned_by="openai", created=1677610602, headers=_llt_headers, gate=_llt_gate, shape=_llt_shape,
                load=lambda m: m in LLT_IDS, new_id=lambda kind: f"{kind}-{uuid.uuid4()}",
                missing=lambda m: _llt_err(f"/chat/completions: Invalid model name passed in model={m}. Call "
                                           "`/v1/models` to view available models for your key.",
                                           "invalid_request_error", 400, "model"))
ROUTERS += [llt_x, make_router(f"{LLT}/v1", LLT_IDS, **_llt_api), make_router(LLT, LLT_IDS, **_llt_api),
            anthropic_compat.make_router(f"{LLT}/v1", gate=_llt_gate, headers=_llt_headers), _tail(LLT, llt_404)]


# ================================================================ llama.cpp server (:8080)
LCP = "/_llamacpp"
LCP_PATH = "/models/Llama-3.3-70B-Instruct-Q4_K_M.gguf"
LCP_BUILD = "b6795-5cca2542"
LCP_CTX = 8192  # slot başına (-c 32768 --parallel 4)
LCP_PARAMS = {"n_predict": -1, "seed": 4294967295, "temperature": 0.8, "dynatemp_range": 0.0,
              "dynatemp_exponent": 1.0, "top_k": 40, "top_p": 0.95, "min_p": 0.05, "top_n_sigma": -1.0,
              "xtc_probability": 0.0, "xtc_threshold": 0.1, "typical_p": 1.0, "repeat_last_n": 64,
              "repeat_penalty": 1.0, "presence_penalty": 0.0, "frequency_penalty": 0.0, "dry_multiplier": 0.0,
              "dry_base": 1.75, "dry_allowed_length": 2, "dry_penalty_last_n": 32768, "mirostat": 0,
              "mirostat_tau": 5.0, "mirostat_eta": 0.1, "stop": [], "max_tokens": -1, "n_keep": 0, "n_discard": 0,
              "ignore_eos": False, "stream": True, "n_probs": 0, "min_keep": 0, "chat_format": "Content-only",
              "reasoning_format": "auto", "samplers": ["penalties", "dry", "top_n_sigma", "top_k", "typ_p", "top_p",
                                                       "min_p", "xtc", "temperature"],
              "speculative.n_max": 16, "speculative.n_min": 0, "speculative.p_min": 0.75,
              "timings_per_token": False, "post_sampling_probs": False, "lora": []}
LCP_TEMPLATE = ("{{- bos_token }}\n{%- if custom_tools is defined %}\n    {%- set tools = custom_tools %}\n{%- endif %}\n"
                "{%- if not tools_in_user_message is defined %}\n    {%- set tools_in_user_message = true %}\n{%- endif %}\n"
                "{%- if not date_string is defined %}\n    {%- set date_string = \"26 Jul 2024\" %}\n{%- endif %}\n"
                "{%- if not tools is defined %}\n    {%- set tools = none %}\n{%- endif %}\n")


def _lcp_slot(i: int) -> dict:
    return {"id": i, "id_task": -1, "n_ctx": LCP_CTX, "speculative": False, "is_processing": False,
            "params": LCP_PARAMS, "next_token": {"has_next_token": True, "has_new_line": False, "n_remain": -1,
                                                 "n_decoded": 0, "stopping_word": ""}}


def _lcp_timings(pt: int, ct: int) -> dict:
    pms, gms = pt / 900 * 1000, max(ct, 1) / 28 * 1000  # GPU'daki 70B izlenimi (Ollama ile aynı hızlar)
    return {"prompt_n": pt, "prompt_ms": round(pms, 3), "prompt_per_token_ms": round(pms / max(pt, 1), 6),
            "prompt_per_second": round(pt / pms * 1000 if pms else 0, 6), "predicted_n": ct,
            "predicted_ms": round(gms, 3), "predicted_per_token_ms": round(gms / max(ct, 1), 6),
            "predicted_per_second": round(max(ct, 1) / gms * 1000, 6)}


def _lcp_shape(o: dict, kind: str) -> dict:
    """llama.cpp (nlohmann::ordered_json) alan sırası: choices önce, id sonda; timings eklenir."""
    if kind == "models":
        m = {"name": LCP_PATH, "model": LCP_PATH, "modified_at": "", "size": "", "digest": "", "type": "model",
             "description": "", "tags": [""], "capabilities": ["completion"], "parameters": "",
             "details": {"parent_model": "", "format": "gguf", "family": "", "families": [""], "parameter_size": "",
                         "quantization_level": ""}}
        return {"models": [m], "object": "list", "data": o["data"]}
    if kind in ("chat", "text"):
        pt, ct = _usage_tokens(o)
        ch = [{"finish_reason": c["finish_reason"], "index": c["index"], "message": c["message"]} if kind == "chat"
              else {"text": c["text"], "index": c["index"], "logprobs": None, "finish_reason": c["finish_reason"]}
              for c in o["choices"]]
        return {"choices": ch, "created": o["created"], "model": o["model"], "system_fingerprint": LCP_BUILD,
                "object": o["object"], "usage": {"completion_tokens": ct, "prompt_tokens": pt, "total_tokens": pt + ct},
                "id": o["id"], "timings": _lcp_timings(pt, ct)}
    if kind in ("chunk", "text_chunk"):
        return {"choices": o["choices"], "created": o["created"], "id": o["id"], "model": o["model"],
                "system_fingerprint": LCP_BUILD, "object": o["object"]}
    return o


def _lcp_err(code: int, msg: str, kind: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": msg, "type": kind}}, code)


lcp_x = APIRouter(prefix=LCP)


@lcp_x.get("/")
async def lcp_ui():
    return HTMLResponse("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<link rel="icon" href="./favicon.svg" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>llama.cpp - AI Chat Interface</title>
</head>
<body data-sveltekit-preload-data="hover">
<div style="display: contents"><noscript>Please enable JavaScript to use llama.cpp web UI.</noscript></div>
</body>
</html>
""")


@lcp_x.get("/health")
@lcp_x.get("/v1/health")
async def lcp_health():
    return {"status": "ok"}


@lcp_x.get("/props")
async def lcp_props(request: Request):
    request.state.tags.add("model_enum")
    return {"default_generation_settings": {**_lcp_slot(0), "prompt": ""}, "total_slots": 4, "model_path": LCP_PATH,
            "modalities": {"vision": False, "audio": False}, "chat_template": LCP_TEMPLATE,
            "bos_token": "<|begin_of_text|>", "eos_token": "<|eot_id|>", "build_info": LCP_BUILD}


@lcp_x.get("/slots")
async def lcp_slots():
    return [_lcp_slot(i) for i in range(4)]


@lcp_x.get("/metrics")
async def lcp_metrics():
    return _lcp_err(501, "This server does not support metrics endpoint. Start it with `--metrics`",
                    "not_supported_error")


@lcp_x.post("/embedding")
@lcp_x.post("/embeddings")
@lcp_x.post("/v1/embeddings")
async def lcp_embeddings():
    return _lcp_err(501, "This server does not support embeddings. Start it with `--embeddings`", "not_supported_error")


@lcp_x.post("/completion")
@lcp_x.post("/completions")
async def lcp_completion(request: Request):
    """Yerel /completion (OpenAI biçimi değil): prompt + n_predict, cevap content."""
    p, st = await _json(request), request.state
    prompt = p.get("prompt")
    prompt = "\n".join(map(str, prompt)) if isinstance(prompt, list) else str(prompt or "")
    n = p.get("n_predict")
    n = n if isinstance(n, int) and n > 0 else None
    st.extra.update(requested_model=LCP_PATH, max_tokens=n)
    # Sohbet modeli "The capital of France is" istemini baştan yazar: bütçeye istemi de kat, baştaki tekrarı at
    msg, tags = await agent.run_raw(st.sid, [{"role": "user", "content": prompt}], model="llama-3.3-70b-instruct",
                                    max_tokens=n and n + ntok(prompt) + 2, temperature=p.get("temperature"))
    st.tags |= tags | {"llm_usage"}
    if msg is None:
        return _lcp_err(503, "Loading model", "unavailable_error")
    text, head = msg["content"], prompt.strip()
    if head and text.lower().startswith(head.lower()):
        text = " " + probes.clip(text[len(head):].lstrip(), n)
    else:
        text = probes.clip(text, n)
    pt, ct = ntok(prompt) + 1, ntok(text)
    final = {"index": 0, "content": text, "tokens": [], "id_slot": 0, "stop": True, "model": "gpt-3.5-turbo",
             "tokens_predicted": ct, "tokens_evaluated": pt, "generation_settings": LCP_PARAMS, "prompt": prompt,
             "has_new_line": "\n" in text, "truncated": False, "stop_type": "eos", "stopping_word": "",
             "tokens_cached": pt + ct, "timings": _lcp_timings(pt, ct)}
    if p.get("stream"):
        async def gen():
            for i, piece in enumerate(pieces(text), 1):
                yield "data: " + dumps({"index": 0, "content": piece, "tokens": [], "stop": False, "id_slot": -1,
                                        "tokens_predicted": i, "tokens_evaluated": pt}) + "\n\n"
            yield "data: " + dumps({**final, "content": ""}) + "\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    return final


async def lcp_404(path: str):
    return _lcp_err(404, "File Not Found", "not_found_error")


_lcp_api = dict(owned_by="llamacpp", created=0, shape=_lcp_shape, new_id=lambda kind: f"{kind}-{_alnum(32)}",
                card=lambda m: {"id": m, "object": "model", "created": int(time.time()), "owned_by": "llamacpp",
                                "meta": {"vocab_type": 2, "n_vocab": 128256, "n_ctx_train": 131072, "n_embd": 8192,
                                         "n_params": 70553706496, "size": 42520393728}},
                busy={"error": {"code": 503, "message": "Loading model", "type": "unavailable_error"}})
# llama.cpp model adını yok sayar (load yok): her ad kabul
ROUTERS += [lcp_x, make_router(f"{LCP}/v1", [LCP_PATH], **_lcp_api), make_router(LCP, [LCP_PATH], **_lcp_api),
            _tail(LCP, lcp_404)]


# ================================================================ Open WebUI (:3000)
OWU = "/_openwebui"
OWU_VERSION = "0.6.34"
OWU_USERS: dict[str, dict] = {}  # e-posta → kullanıcı; bellekte (yeniden başlatmada token'lar da geçersizleşir)
OWU_SECRET = os.urandom(32)
OWU_BAD_LOGIN = "The email or password provided is incorrect. Please check for typos and try logging in again."


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _sig(msg: str) -> str:
    return _b64u(hmac.new(OWU_SECRET, msg.encode(), hashlib.sha256).digest())


def _owu_token(uid: str) -> str:
    h = _b64u(b'{"alg":"HS256","typ":"JWT"}')
    p = _b64u(dumps({"id": uid, "exp": int(time.time()) + 28 * 86400, "jti": str(uuid.uuid4())}).encode())
    return f"{h}.{p}.{_sig(f'{h}.{p}')}"


def _owu_user(request: Request) -> dict | None:
    tok = api_key(request) or request.cookies.get("token", "")
    if LLM_KEY_MARK in tok:  # .env'deki işaretli anahtarı API anahtarı diye denedi
        return {"id": "a3f1c2d4-0b5e-4c6f-9a7d-1e2f3a4b5c6d", "email": "ops@sepetly.example", "name": "ops",
                "role": "admin", "profile_image_url": "/user.png"}
    parts = tok.split(".")
    if len(parts) != 3 or not hmac.compare_digest(parts[2], _sig(f"{parts[0]}.{parts[1]}")):
        return None
    try:
        uid = json.loads(base64.urlsafe_b64decode(parts[1] + "==")).get("id")
    except ValueError:
        return None
    return next((u for u in OWU_USERS.values() if u["id"] == uid), None)


def _owu_gate(request: Request):
    if _owu_user(request):
        return None
    detail = ("Your session has expired or the token is invalid. Please sign in again." if api_key(request)
              else "Not authenticated")
    return JSONResponse({"detail": detail}, 401)


def _owu_public(u: dict, token: str | None = None) -> dict:
    out = {"id": u["id"], "email": u["email"], "name": u["name"], "role": u["role"],
           "profile_image_url": u["profile_image_url"]}
    if token:
        out.update(token=token, token_type="Bearer", expires_at=int(time.time()) + 28 * 86400)
    ws = u["role"] == "admin"
    out["permissions"] = {"workspace": {"models": ws, "knowledge": ws, "prompts": ws, "tools": ws},
                          "chat": {"controls": True, "file_upload": True, "delete": True, "edit": True,
                                   "temporary": True}, "features": {"web_search": True, "image_generation": True,
                                                                    "code_interpreter": True}}
    return out


def _owu_card(m: str) -> dict:
    n = ollama_compat._canon(m)
    _, size, fam, ps = ollama_compat.MOD[n]
    return {"id": n, "name": n, "object": "model", "created": int(time.time()), "owned_by": "ollama",
            "ollama": {"name": n, "model": n, "modified_at": ollama_compat._rfc3339(ollama_compat._mod_ns(n)),
                       "size": size, "digest": hashlib.sha256(n.encode()).hexdigest(),
                       "details": ollama_compat._details(n), "connection_type": "local", "urls": [0]},
            "connection_type": "local", "tags": [], "actions": [], "filters": []}


OWU_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<link rel="icon" type="image/png" href="/static/favicon.png" />
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover" />
<meta name="theme-color" content="#171717" />
<meta name="robots" content="noindex,nofollow" />
<meta name="description" content="Open WebUI" />
<link rel="manifest" href="/manifest.json" crossorigin="use-credentials" />
<title>Open WebUI</title>
<style>
body{margin:0;background:#171717;color:#eee;font-family:ui-sans-serif,system-ui,sans-serif;display:flex;
justify-content:center;align-items:center;min-height:100vh}
form{width:340px;display:flex;flex-direction:column;gap:12px}h1{font-size:22px;font-weight:500;text-align:center}
input{background:#262626;border:0;border-radius:12px;padding:12px;color:#eee}
button{background:#fff;color:#000;border:0;border-radius:999px;padding:11px;font-weight:500;cursor:pointer}
#msg{color:#f87171;font-size:13px;min-height:16px;text-align:center}a{color:#aaa;font-size:13px;text-align:center}
</style>
</head>
<body>
<form id="f"><h1 id="t">Sign in to Open WebUI</h1>
<input id="name" placeholder="Enter Your Full Name" hidden />
<input id="email" type="email" placeholder="Enter Your Email" autocomplete="email" required />
<input id="password" type="password" placeholder="Enter Your Password" autocomplete="current-password" required />
<button id="b">Sign in</button><div id="msg"></div>
<a href="#" id="sw">Don't have an account? Sign up</a></form>
<script>
let up=false;const $=i=>document.getElementById(i);
$('sw').onclick=e=>{e.preventDefault();up=!up;$('name').hidden=!up;$('t').textContent=up?'Sign up to Open WebUI':'Sign in to Open WebUI';
  $('b').textContent=up?'Create Account':'Sign in';$('sw').textContent=up?'Already have an account? Sign in':"Don't have an account? Sign up"};
$('f').onsubmit=async e=>{e.preventDefault();const body={email:$('email').value,password:$('password').value};if(up)body.name=$('name').value;
  const r=await fetch('/api/v1/auths/'+(up?'signup':'signin'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();if(d.token){localStorage.token=d.token;$('msg').textContent='';location.reload()}else $('msg').textContent=d.detail||'Error'};
</script>
</body>
</html>
"""

owu_x = APIRouter(prefix=OWU)


@owu_x.get("/")
@owu_x.get("/auth")
async def owu_index():
    return HTMLResponse(OWU_PAGE)


@owu_x.get("/api/config")
async def owu_config(request: Request):
    return {"status": True, "name": "Open WebUI", "version": OWU_VERSION, "default_locale": "",
            "oauth": {"providers": {}},
            "features": {"auth": True, "auth_trusted_header": False, "enable_ldap": False, "enable_api_key": True,
                         "enable_signup": True, "enable_login_form": True, "enable_websocket": True,
                         "enable_version_update_check": True}}


@owu_x.get("/api/version")
async def owu_version():
    return {"version": OWU_VERSION}


@owu_x.get("/health")
@owu_x.get("/health/db")
async def owu_health():
    return {"status": True}


@owu_x.get("/manifest.json")
async def owu_manifest():
    return {"name": "Open WebUI", "short_name": "Open WebUI", "description": "Open WebUI is an open, extensible, "
            "user-friendly interface for AI that adapts to your workflow.", "start_url": "/", "display": "standalone",
            "background_color": "#343541", "icons": [{"src": "/static/logo.png", "type": "image/png", "sizes": "500x500",
                                                     "purpose": "any"}]}


@owu_x.post("/api/v1/auths/signin")
async def owu_signin(request: Request):
    request.state.tags.add("credential_capture")
    p = await _json(request)
    u = OWU_USERS.get(str(p.get("email", "")).strip().lower())
    pw = hashlib.sha256(str(p.get("password", "")).encode()).hexdigest()
    if not u or not hmac.compare_digest(u["pw"], pw):
        return JSONResponse({"detail": OWU_BAD_LOGIN}, 400)
    return _owu_public(u, _owu_token(u["id"]))


@owu_x.post("/api/v1/auths/signup")
async def owu_signup(request: Request):
    """Kayıt açık; gerçek Open WebUI ilk kaydı admin yapar. Her kayıt kendini ilk kullanıcı sanar (admin):
    Functions üzerinden kod çalıştırma zincirinin (create → toggle → chat) sonuna kadar gelsin."""
    st = request.state
    st.tags |= {"credential_capture", "account_signup"}
    p = await _json(request)
    email = str(p.get("email", "")).strip().lower()
    if "@" not in email or not p.get("password"):
        return JSONResponse({"detail": "Invalid email format"}, 400)
    if email in OWU_USERS:
        return JSONResponse({"detail": "Uh-oh! This email is already registered. Sign in with your existing account "
                                       "or choose another email to start anew."}, 400)
    u = {"id": str(uuid.uuid4()), "email": email, "name": str(p.get("name") or email.split("@")[0])[:64],
         "role": "admin", "profile_image_url": "/user.png", "pw": hashlib.sha256(str(p["password"]).encode()).hexdigest()}
    if len(OWU_USERS) > 2000:
        OWU_USERS.pop(next(iter(OWU_USERS)))
    OWU_USERS[email] = u
    return _owu_public(u, _owu_token(u["id"]))


@owu_x.get("/api/v1/auths/")
async def owu_me(request: Request):
    if deny := _owu_gate(request):
        return deny
    return _owu_public(_owu_user(request))


# --- Admin yapılandırma uçları: gerçekte bağlı OpenAI / Ollama anahtarlarını döndürür. Burada işaretli yem anahtar;
# base URL bizim relay'e gider, anahtar geri gelirse owu_config_key_reuse
def _owu_conns():
    gw = settings.BAIT_DOMAIN or "llm-gw.sepetly.example"
    return f"https://{gw}/v1", f"http://{gw}:11434"


def _owu_openai_cfg() -> dict:
    url, _ = _owu_conns()
    return {"ENABLE_OPENAI_API": True, "OPENAI_API_BASE_URLS": [url], "OPENAI_API_KEYS": [tools.OWU_OPENAI_KEY],
            "OPENAI_API_CONFIGS": {"0": {"enable": True, "tags": [], "prefix_id": "", "model_ids": [],
                                         "connection_type": "external"}}}


def _owu_ollama_cfg() -> dict:
    _, url = _owu_conns()
    return {"ENABLE_OLLAMA_API": True, "OLLAMA_BASE_URLS": [url], "OLLAMA_API_CONFIGS": {}}


def _owu_cfg_read(request: Request):
    request.state.tags |= {"owu_config_read", "secret_bait_served"}
    return _owu_admin(request)[1]


@owu_x.get("/openai/config")
async def owu_openai_cfg(request: Request):
    return _owu_cfg_read(request) or _owu_openai_cfg()


@owu_x.get("/ollama/config")
async def owu_ollama_cfg(request: Request):
    return _owu_cfg_read(request) or _owu_ollama_cfg()


@owu_x.post("/openai/config/update")
@owu_x.post("/ollama/config/update")
async def owu_cfg_update(request: Request):
    """Bağlantıyı kendi sunucusuna çevirmek (sohbetleri dinlemek) isteyebilir: kaydedilir, uygulanmaz."""
    request.state.tags.add("owu_config_read")
    if deny := _owu_admin(request)[1]:
        return deny
    p = await _json(request)
    base = _owu_openai_cfg() if request.url.path.startswith(f"{OWU}/openai") else _owu_ollama_cfg()
    return base | {k: v for k, v in p.items() if k in base}


@owu_x.get("/api/v1/configs/export")
async def owu_cfg_export(request: Request):
    if deny := _owu_cfg_read(request):
        return deny
    o, ol = _owu_openai_cfg(), _owu_ollama_cfg()
    return {"version": 0, "ui": {"enable_signup": True, "default_locale": "", "default_models": "llama3.1:70b"},
            "openai": {"enable": True, "api_base_urls": o["OPENAI_API_BASE_URLS"], "api_keys": o["OPENAI_API_KEYS"],
                       "api_configs": o["OPENAI_API_CONFIGS"]},
            "ollama": {"enable": True, "base_urls": ol["OLLAMA_BASE_URLS"], "api_configs": {}},
            "auth": {"admin": {"show": True}, "jwt_expiry": "4w", "api_key": {"enable": True}}}


@owu_x.get("/api/v1/configs/connections")
async def owu_cfg_conns(request: Request):
    return _owu_admin(request)[1] or {"ENABLE_DIRECT_CONNECTIONS": False, "ENABLE_BASE_MODELS_CACHE": False}


@owu_x.get("/api/v1/audio/config")
async def owu_audio_cfg(request: Request):
    if deny := _owu_cfg_read(request):
        return deny
    url, k = _owu_conns()[0], tools.OWU_OPENAI_KEY
    return {"tts": {"OPENAI_API_BASE_URL": url, "OPENAI_API_KEY": k, "API_KEY": "", "ENGINE": "openai",
                    "MODEL": "tts-1", "VOICE": "alloy", "SPLIT_ON": "punctuation", "AZURE_SPEECH_REGION": "",
                    "AZURE_SPEECH_OUTPUT_FORMAT": ""},
            "stt": {"OPENAI_API_BASE_URL": url, "OPENAI_API_KEY": k, "ENGINE": "openai", "MODEL": "whisper-1",
                    "WHISPER_MODEL": "base", "DEEPGRAM_API_KEY": "", "AZURE_API_KEY": "", "AZURE_REGION": "",
                    "AZURE_LOCALES": ""}}


@owu_x.get("/api/v1/images/config")
async def owu_images_cfg(request: Request):
    if deny := _owu_cfg_read(request):
        return deny
    return {"enabled": False, "engine": "openai", "prompt_generation": True,
            "openai": {"OPENAI_API_BASE_URL": _owu_conns()[0], "OPENAI_API_KEY": tools.OWU_OPENAI_KEY},
            "automatic1111": {"AUTOMATIC1111_BASE_URL": "", "AUTOMATIC1111_API_AUTH": "", "AUTOMATIC1111_CFG_SCALE": None,
                              "AUTOMATIC1111_SAMPLER": None, "AUTOMATIC1111_SCHEDULER": None},
            "comfyui": {"COMFYUI_BASE_URL": "", "COMFYUI_API_KEY": "", "COMFYUI_WORKFLOW": "",
                        "COMFYUI_WORKFLOW_NODES": []},
            "gemini": {"GEMINI_API_BASE_URL": "", "GEMINI_API_KEY": ""}}


OWU_FUNCS: dict[str, dict] = {}  # kimlik → Function (bellekte; kod asla çalıştırılmaz, yalnız kaydedilir)
OWU_NO_PERM = {"detail": "You do not have permission to access this resource. Please ask your administrator for "
                         "assistance."}
FUNC_TYPE = re.compile(r"^class\s+(Pipe|Filter|Action)\b", re.M)


def _owu_admin(request: Request):
    """Functions yalnız admin'e açık (gerçekte get_admin_user): token yok → 401, user rolü → 401 izin hatası."""
    if deny := _owu_gate(request):
        return None, deny
    u = _owu_user(request)
    return (u, None) if u["role"] == "admin" else (None, JSONResponse(OWU_NO_PERM, 401))


def _func_out(f: dict) -> dict:
    return {k: v for k, v in f.items() if k != "content"} | {"content": f["content"]}


@owu_x.get("/api/v1/functions/")
@owu_x.get("/api/v1/functions/export")
async def owu_funcs(request: Request):
    u, deny = _owu_admin(request)
    if deny:
        return deny
    return [_func_out(f) for f in OWU_FUNCS.values() if f["user_id"] == u["id"]]


@owu_x.post("/api/v1/functions/create")
async def owu_func_create(request: Request):
    st = request.state
    st.tags.add("function_upload")
    u, deny = _owu_admin(request)
    if deny:
        return deny
    p = await _json(request)
    fid, content = str(p.get("id", "")).lower(), str(p.get("content", ""))
    if not fid.isidentifier():
        return JSONResponse({"detail": "Only alphanumeric characters and underscores are allowed in the id"}, 400)
    if fid in OWU_FUNCS:
        return JSONResponse({"detail": "Uh-oh! This id is already registered. Please choose another id string."}, 400)
    if not (m := FUNC_TYPE.search(content)):
        return JSONResponse({"detail": "No Function class found in the module"}, 400)
    now = int(time.time())
    meta = p.get("meta") if isinstance(p.get("meta"), dict) else {}
    f = {"id": fid, "user_id": u["id"], "name": str(p.get("name") or fid)[:128], "type": m[1].lower(),
         "content": content, "meta": {"description": meta.get("description", ""), "manifest": {}},
         "is_active": False, "is_global": False, "updated_at": now, "created_at": now}
    if len(OWU_FUNCS) > 500:
        OWU_FUNCS.pop(next(iter(OWU_FUNCS)))
    OWU_FUNCS[fid] = f
    st.extra.update(function_id=fid, function_type=f["type"])
    return _func_out(f)


OWU_NOT_FOUND = {"detail": "We could not find what you're looking for :/"}
PY_TYPES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "list": "array", "dict": "object"}


def _valves_spec(content: str, cls: str) -> dict | None:
    """Kod çalıştırılmadan `class Valves(BaseModel)` gövdesinden pydantic şeması (alan adı + tür) çıkarılır."""
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if m := re.match(rf"(\s*)class {cls}\b", line):
            break
    else:
        return None
    props = {}
    for line in lines[i + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= len(m[1]):
            break
        if fm := re.match(r"\s+(\w+)\s*:\s*([A-Za-z_]\w*)", line):
            props[fm[1]] = {"title": fm[1].replace("_", " ").title(), "type": PY_TYPES.get(fm[2].lower(), "string")}
    return {"properties": props, "title": cls, "type": "object"}


@owu_x.get("/api/v1/functions/id/{fid}")
@owu_x.post("/api/v1/functions/id/{fid}/toggle")
@owu_x.post("/api/v1/functions/id/{fid}/toggle/global")
@owu_x.delete("/api/v1/functions/id/{fid}/delete")
@owu_x.post("/api/v1/functions/id/{fid}/update")
@owu_x.get("/api/v1/functions/id/{fid}/valves")
@owu_x.get("/api/v1/functions/id/{fid}/valves/spec")
@owu_x.post("/api/v1/functions/id/{fid}/valves/update")
@owu_x.get("/api/v1/functions/id/{fid}/valves/user")
@owu_x.get("/api/v1/functions/id/{fid}/valves/user/spec")
@owu_x.post("/api/v1/functions/id/{fid}/valves/user/update")
async def owu_func(fid: str, request: Request):
    u, deny = _owu_admin(request)
    if deny:
        return deny
    f = OWU_FUNCS.get(fid)
    if not f or f["user_id"] != u["id"]:
        return JSONResponse(OWU_NOT_FOUND, 401)
    request.state.extra.update(function_id=fid)
    path = request.url.path
    if path.endswith("/delete"):
        OWU_FUNCS.pop(fid, None)
        return True
    if "/valves" in path:
        user = "/valves/user" in path
        key, cls = ("user_valves", "UserValves") if user else ("valves", "Valves")
        if path.endswith("/spec"):
            return _valves_spec(f["content"], cls)
        if path.endswith("/update"):
            if _valves_spec(f["content"], cls) is None:
                return JSONResponse(OWU_NOT_FOUND, 401)
            p = await _json(request)
            f[key] = {k: v for k, v in p.items() if isinstance(k, str)}
            return f[key]
        return f.get(key) or {}
    if path.endswith("/update"):  # gerçekte kod yeniden yüklenir; burada yalnız kaydedilir
        request.state.tags.add("function_upload")
        p = await _json(request)
        content = str(p.get("content", ""))
        if not (m := FUNC_TYPE.search(content)):
            return JSONResponse({"detail": "No Function class found in the module"}, 400)
        meta = p.get("meta") if isinstance(p.get("meta"), dict) else {}
        f.update(name=str(p.get("name") or f["name"])[:128], content=content, type=m[1].lower(),
                 meta={"description": meta.get("description", ""), "manifest": {}}, updated_at=int(time.time()))
        request.state.extra.update(function_type=f["type"])
        return _func_out(f)
    if path.endswith("/toggle/global"):
        f["is_global"] = not f["is_global"]
    elif path.endswith("/toggle"):
        request.state.tags.add("function_upload")
        f["is_active"] = not f["is_active"]
    if path.endswith(("/toggle", "/global")):
        f["updated_at"] = int(time.time())
    return _func_out(f)


MANIFOLD = re.compile(r"\bself\.pipes\s*=|\bdef\s+pipes\s*\(|^\s+pipes\s*=", re.M)


def _owu_pipe(model: str) -> dict | None:
    """Model adı etkin bir pipe mı: `fid` ya da manifold pipe'ın alt modeli `fid.alt` (alt adlar koddan gelir,
    kod çalıştırılmadığı için her alt ad kabul edilir)."""
    f = OWU_FUNCS.get(model)
    if not f and "." in model:
        f = OWU_FUNCS.get(model.split(".", 1)[0])
        f = f if f and MANIFOLD.search(f["content"]) else None
    return f if f and f["is_active"] and f["type"] == "pipe" else None


@owu_x.get("/api/models")
async def owu_models(request: Request):
    """Gerçek Open WebUI etkin pipe'ları da model diye listeler; yalnız kendi yüklediği görünür."""
    request.state.tags.add("model_enum")
    if deny := _owu_gate(request):
        return deny
    uid = _owu_user(request)["id"]
    pipes = [{"id": f["id"], "name": f["name"], "object": "model", "created": f["created_at"], "owned_by": "openai",
              "pipe": {"type": "pipe"}, "tags": [], "actions": [], "filters": []}
             for f in OWU_FUNCS.values() if f["user_id"] == uid and f["is_active"] and f["type"] == "pipe"]
    return {"data": [_owu_card(m) for m in OWU_MODELS] + pipes}


def _owu_missing(model: str):
    """Etkin pipe'ın kimliği model diye istendiyse: kod çalıştırılmaz, pipe boş döndürmüş gibi cevap verilir."""
    if not _owu_pipe(model):
        return JSONResponse({"detail": "Model not found"}, 404)
    return {"id": f"{model}-{uuid.uuid4()}", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "logprobs": None, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": ""}}], "object": "chat.completion"}


async def owu_404(path: str):
    # API dışındaki her yol SPA'nın index'ini döner (SvelteKit istemci yönlendirmesi)
    return JSONResponse(NOT_FOUND, 404) if path.startswith(("api/", "ollama/", "openai/")) else HTMLResponse(OWU_PAGE)


OWU_MODELS = [m[0] for m in ollama_compat.MODELS]
ROUTERS += [owu_x, make_router(
    f"{OWU}/api", OWU_MODELS, owned_by="ollama", created=0, card=_owu_card, gate=_owu_gate,
    shape=lambda o, kind: {"data": o["data"]} if kind == "models" else o, load=ollama_compat.load,
    refuse=ollama_compat.refuse,
    embed_dim=ollama_compat._dim, missing=_owu_missing,
    new_id=lambda kind: f"{kind}-{uuid.uuid4()}"), _tail(OWU, owu_404)]


# ================================================================ Gradio / text-generation-webui (:7860)
GR = "/_gradio"
GR_CONFIG = {"version": "4.37.2", "mode": "blocks", "app_id": 4518021733295183000, "dev_mode": False,
             "analytics_enabled": False, "components": [], "css": None, "connect_heartbeat": False, "js": None,
             "head": None, "title": "Text generation web UI", "space_id": None, "enable_queue": True,
             "show_error": False, "show_api": False, "is_colab": False, "max_file_size": None, "stylesheets": [],
             "theme": "default", "protocol": "sse_v3", "body_css": {"body_background_fill": "white",
                                                                    "body_text_color": "#1f2937"},
             "fill_height": False, "theme_hash": "3b5d1c4e0e3b96c4b3b0a1f1b67f8e5c2fbe0d2a6c6d1b1e5a6f0b9d4c7e2a11",
             "layout": {"id": 0, "children": []}, "dependencies": [], "root": "", "username": None}

gr_x = APIRouter(prefix=GR)


@gr_x.get("/")
async def gr_index():
    return HTMLResponse(f"""<!doctype html>
<html lang="en" style="margin: 0; padding: 0; min-height: 100%; display: flex; flex-direction: column;">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no, maximum-scale=1" />
<meta property="og:title" content="Gradio" />
<title>Text generation web UI</title>
<script>window.gradio_config = {json.dumps(GR_CONFIG)};</script>
<script type="module" crossorigin src="./assets/index-BMXBN1nU.js"></script>
</head>
<body style="width: 100%; margin: 0; padding: 0; display: flex; flex-direction: column; flex-grow: 1">
<gradio-app control_page_title="true" embed="false" eager="true" style="display: flex; flex-direction: column; flex-grow: 1">
</gradio-app>
</body>
</html>
""")


@gr_x.get("/config")
async def gr_config():
    return GR_CONFIG


@gr_x.get("/info")
async def gr_info():
    return {"named_endpoints": {}, "unnamed_endpoints": {}}


@gr_x.post("/queue/join")
@gr_x.post("/gradio_api/queue/join")
async def gr_join(request: Request):
    request.state.tags.add("gradio_api")
    return {"event_id": uuid.uuid4().hex}


@gr_x.get("/queue/data")
@gr_x.get("/gradio_api/queue/data")
async def gr_data():
    async def gen():
        yield "data: " + json.dumps({"msg": "unexpected_error", "message": "Session not found.", "success": False}) + "\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


@gr_x.get("/file={path:path}")
@gr_x.get("/gradio_api/file={path:path}")
async def gr_file(path: str, request: Request):
    request.state.tags.add("gradio_api")  # CVE-2023-51449 / CVE-2024-1561 tarzı dosya okuma denemesi
    return JSONResponse({"detail": f"File not allowed: {path}."}, 403)


async def gr_404(path: str, request: Request):
    if request.method == "POST" and path.startswith(("run/", "api/", "call/", "gradio_api/")):
        request.state.tags.add("gradio_api")
    return JSONResponse(NOT_FOUND, 404)


ROUTERS += [gr_x, _tail(GR, gr_404)]
