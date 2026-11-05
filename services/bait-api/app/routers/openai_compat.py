import hashlib
import json
import math
import random
import re
import string
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..engine import agent

# Reverse-proxy görünümü: popüler modeller tek uçtan
MODELS = ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "o3-mini", "claude-sonnet-4", "claude-3-5-haiku",
          "llama-3.1-70b-instruct", "deepseek-r1", "text-embedding-3-small"]
BUSY = {"error": {"message": "The server is overloaded or not ready yet.", "type": "server_error",
                  "param": None, "code": None}}
PIECE = re.compile(r"\s*\S+|\s+")


class GinJSON(JSONResponse):
    """Ollama (Go/Gin) JSON'u charset ile döner; Starlette'in yalın 'application/json'u ele verir."""
    media_type = "application/json; charset=utf-8"


def _hdrs():
    return {"x-request-id": f"req_{uuid.uuid4().hex[:24]}", "openai-processing-ms": "812",
            "x-ratelimit-limit-requests": "10000", "x-ratelimit-remaining-requests": "9987",
            "x-ratelimit-limit-tokens": "2000000", "x-ratelimit-remaining-tokens": "1994210",
            "x-proxy": "spt-relay/1.4"}


def api_key(request: Request) -> str:
    """İstemcinin anahtarı: OpenAI (Bearer), Anthropic (x-api-key), Azure/LiteLLM (api-key), Gemini (x-goog-api-key, ?key=)."""
    h = request.headers
    auth = h.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        auth = auth[7:].strip()
    return (auth or h.get("x-api-key") or h.get("api-key") or h.get("x-goog-api-key")
            or request.query_params.get("key") or "")


def ntok(text: str) -> int:
    return max(1, len(text or "") // 4)


def dumps(obj) -> str:
    """Boşluksuz JSON: Go ve OpenAI böyle yollar, json.dumps varsayılanındaki ', ' / ': ' ele verir."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def pieces(text: str) -> list[str]:
    """Akış parçaları token benzeri kelime dilimleri (sabit 24 karakter kelime ortasından kesiyordu)."""
    return PIECE.findall(text or "")


def embedding(text: str, dim: int) -> list[float]:
    """Metne göre deterministik, birim uzunlukta sahte vektör (aynı metin → aynı vektör)."""
    raw = b"".join(hashlib.sha256(f"{i}|{text}".encode()).digest() for i in range(dim // 32 + 1))[:dim]
    v = [b / 255 - 0.5 for b in raw]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [round(x / n, 6) for x in v]


def make_router(prefix: str, models: list[str], *, owned_by: str, created, headers=dict, ollama: bool = False,
                embed_dim=lambda m: 3072 if "large" in m else 1536, busy: dict = BUSY, load=None,
                card=None, shape=None, gate=None, missing=None, new_id=None, alt_models=None, json_cls=None,
                refuse=None) -> APIRouter:
    """OpenAI uyumlu uçlar (ham mod). Aynı kod birçok yüzeyde: 443'teki relay (/v1), Ollama'nın /v1'i (:11434) ve
    llm_servers'taki vLLM / LM Studio / LiteLLM / llama.cpp / Open WebUI taklitleri.
    created: sabit ya da model → unix zamanı; load: verilirse kurulu olmayan model `missing` (relay her adı kabul eder).
    Sunucu farkları kancalarla: card(model) → kart, shape(gövde, tür) → ek alan / alan sırası (tür: models, chat,
    chunk, text, text_chunk), gate(request) → yetki hatası ya da None, missing(model) → yanıt, new_id(tür) → kimlik,
    alt_models(request) → model listesi başka biçimde istendiyse (ör. Anthropic SDK) yanıt; json_cls: yanıt sınıfı;
    refuse(model, işlem) → modelin desteklemediği işlemde hata metni (ör. embedding modeliyle sohbet)."""
    r = APIRouter(prefix=prefix)
    J = json_cls or (GinJSON if ollama else JSONResponse)
    shape = shape or (lambda obj, kind: obj)

    def default_card(m):
        return {"id": m, "object": "model", "created": created(m) if callable(created) else created,
                "owned_by": owned_by}

    card = card or default_card

    def bad(msg):
        return J({"error": {"message": msg, "type": "invalid_request_error", "param": None, "code": None}}, 400)

    def default_missing(model):
        return J({"error": {"message": f'model "{model}" not found, try pulling it first', "type": "not_found_error",
                            "param": None, "code": None}}, 404)

    missing = missing or default_missing

    def default_id(kind):  # Ollama kimliği chatcmpl-<0..998> (Go: rand.Intn(999))
        return f"{kind}-{random.randrange(999)}" if ollama else f"{kind}-{uuid.uuid4().hex[:24]}"

    new_id = new_id or default_id

    def head(kind, obj, model):
        # Alan sırası gerçek sunucularla aynı
        h = {"id": new_id(kind), "object": obj, "created": int(time.time()), "model": model}
        if ollama:
            h["system_fingerprint"] = "fp_ollama"
        return h

    def call_id():  # Ollama: call_ + 8 küçük harf/rakam
        if ollama:
            return "call_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        return f"call_{uuid.uuid4().hex[:24]}"

    async def payload_of(request: Request) -> dict | None:
        try:
            p = await request.json()
        except Exception:
            return None
        return p if isinstance(p, dict) else None

    def note(request: Request, payload: dict, model: str):
        request.state.extra.update(requested_model=model, max_tokens=payload.get("max_tokens"),
                                   api_key=api_key(request)[:200])

    def sse(chunks):
        return StreamingResponse(chunks, media_type="text/event-stream", headers=headers())

    @r.get("/models")
    async def list_models(request: Request):
        request.state.tags.add("model_enum")
        if gate and (deny := gate(request)):
            return deny
        if alt_models and (alt := alt_models(request)):
            return alt
        return J(shape({"object": "list", "data": [card(m) for m in models]}, "models"), headers=headers())

    @r.get("/models/{model:path}")
    async def get_model(model: str, request: Request):
        request.state.tags.add("model_enum")
        if gate and (deny := gate(request)):
            return deny
        if alt_models and (alt := alt_models(request, model)):
            return alt
        if load and not load(model):
            return missing(model)
        return J(card(model), headers=headers())

    @r.post("/chat/completions")
    async def chat(request: Request):
        if gate and (deny := gate(request)):
            return deny
        payload = await payload_of(request)
        if payload is None:
            return bad("Invalid JSON body")
        msgs = agent.normalize(payload.get("messages"))
        if not msgs:
            return bad("messages is required")
        st, model = request.state, str(payload.get("model") or models[0])
        note(request, payload, model)
        if load and not load(model):
            return missing(model)
        if refuse and (err := refuse(model, "chat")):
            return bad(err)
        if any(m["role"] == "system" for m in msgs):  # saldırganın / LLMjacking müşterisinin system prompt'u
            st.tags.add("custom_system_prompt")
        msg, tags = await agent.run_raw(st.sid, msgs, model=model, tools=payload.get("tools"),
                                        max_tokens=payload.get("max_completion_tokens") or payload.get("max_tokens"),
                                        temperature=payload.get("temperature"))
        st.tags |= tags | {"llm_usage"}
        if msg is None:
            return J(busy, 503, headers=headers())
        text = msg["content"]
        calls = [{"id": call_id(), "type": "function",
                  "function": {"name": c["name"], "arguments": dumps(c["arguments"])}}
                 for c in msg.get("tool_calls") or []]
        finish = "tool_calls" if calls else "stop"
        pt, ct = sum(ntok(m["content"]) for m in msgs), ntok(text) + 12 * len(calls)
        usage = {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}
        base = head("chatcmpl", "chat.completion", model)

        if payload.get("stream"):
            def chunk(delta, reason=None):
                return "data: " + dumps(shape({**base, "object": "chat.completion.chunk", "choices": [
                    {"index": 0, "delta": delta, "finish_reason": reason}]}, "chunk")) + "\n\n"

            def delta(**kw):  # Ollama her parçada role+content taşır; OpenAI yalnız değişeni (rol ilk parçada)
                return {"role": "assistant", "content": "", **kw} if ollama else kw

            async def gen():
                if not ollama:
                    yield chunk({"role": "assistant", "content": ""})
                if calls:
                    yield chunk(delta(tool_calls=[{"index": i, **c} for i, c in enumerate(calls)]))
                for p in pieces(text):
                    yield chunk(delta(content=p))
                yield chunk(delta(), finish)
                opts = payload.get("stream_options")
                if isinstance(opts, dict) and opts.get("include_usage"):
                    yield "data: " + dumps({**base, "object": "chat.completion.chunk", "choices": [],
                                            "usage": usage}) + "\n\n"
                yield "data: [DONE]\n\n"
            return sse(gen())

        # tool çağrısında OpenAI content=null, Ollama "" döner
        message = {"role": "assistant", "content": text if text or not calls else ("" if ollama else None)}
        if calls:
            message["tool_calls"] = calls
        return J(shape({**base, "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                        "usage": usage}, "chat"), headers=headers())

    @r.post("/completions")
    async def completions(request: Request):
        if gate and (deny := gate(request)):
            return deny
        payload = await payload_of(request)
        if payload is None:
            return bad("Invalid JSON body")
        prompt = payload.get("prompt")
        prompt = "\n".join(map(str, prompt)) if isinstance(prompt, list) else str(prompt or "")
        st, model = request.state, str(payload.get("model") or models[0])
        note(request, payload, model)
        if load and not load(model):
            return missing(model)
        if refuse and (err := refuse(model, "completions")):
            return bad(err)
        msg, tags = await agent.run_raw(st.sid, [{"role": "user", "content": prompt}], model=model,
                                        max_tokens=payload.get("max_tokens"), temperature=payload.get("temperature"))
        st.tags |= tags | {"llm_usage"}
        if msg is None:
            return J(busy, 503, headers=headers())
        text = msg["content"]
        base = head("cmpl", "text_completion", model)

        def choice(t, reason):
            return {**base, "choices": [{"text": t, "index": 0, "logprobs": None, "finish_reason": reason}]}

        if payload.get("stream"):
            async def gen():
                for p in pieces(text):
                    yield "data: " + dumps(shape(choice(p, None), "text_chunk")) + "\n\n"
                yield "data: " + dumps(shape(choice("", "stop"), "text_chunk")) + "\n\n"
                yield "data: [DONE]\n\n"
            return sse(gen())
        pt, ct = ntok(prompt), ntok(text)
        return J(shape({**choice(text, "stop"), "usage": {"prompt_tokens": pt, "completion_tokens": ct,
                                                          "total_tokens": pt + ct}}, "text"), headers=headers())

    @r.post("/embeddings")
    async def embeddings(request: Request):
        if gate and (deny := gate(request)):
            return deny
        payload = await payload_of(request)
        if payload is None:
            return bad("Invalid JSON body")
        inp = payload.get("input")
        items = [str(x) for x in (inp if isinstance(inp, list) else [inp or ""])][:64]
        model = str(payload.get("model") or models[-1])
        note(request, payload, model)
        if load and not load(model):
            return missing(model)
        request.state.tags.add("llm_usage")
        dim = payload.get("dimensions")
        dim = dim if isinstance(dim, int) and 0 < dim <= 8192 else embed_dim(model)
        n = sum(ntok(t) for t in items)
        return J({"object": "list", "data": [{"object": "embedding", "index": i, "embedding": embedding(t, dim)}
                                             for i, t in enumerate(items)],
                  "model": model, "usage": {"prompt_tokens": n, "total_tokens": n}}, headers=headers())

    return r


# Relay arkasında gerçekten var olan aileler; uydurma ad (key doğrulayıcının "gpt-6-luna"sı) 404 alır
RELAY_KNOWN = re.compile(r"(gpt-(3\.5|4|5)(?![0-9])|chatgpt-|o[134](-|$)|text-embedding-|claude-|"
                         r"llama-?3|meta-llama/|deepseek-|qwen|mistral)[\w.:/-]*$", re.I)


def _relay_missing(model: str):
    return JSONResponse({"error": {"message": f"The model `{model}` does not exist or you do not have access to it.",
                                   "type": "invalid_request_error", "param": None, "code": "model_not_found"}}, 404,
                        headers=_hdrs())


def _anthropic_models(request: Request, model: str | None = None):
    from .anthropic_compat import models_alt  # döngüsel içe aktarma: anthropic_compat bu modülü kullanır
    return models_alt(request, model)


# 443'teki relay: Anthropic SDK'sı aynı /v1/models'ı kendi biçiminde ister (key doğrulayıcıların ilk çağrısı)
router = make_router("/v1", MODELS, owned_by="system", created=1727740800, headers=_hdrs, alt_models=_anthropic_models,
                     load=lambda m: m in MODELS or bool(RELAY_KNOWN.match(m)), missing=_relay_missing)
