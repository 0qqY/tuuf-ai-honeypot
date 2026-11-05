"""Gemini API (generativelanguage /v1beta) taklidi, 443'teki relay'de. .env yemindeki GOOGLE_GEMINI_BASE_URL
buraya döner; generateContent / streamGenerateContent ham moda (agent.run_raw) gider."""
import base64
import json
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..engine import agent
from .openai_compat import api_key, dumps, embedding, ntok

router = APIRouter(prefix="/v1beta")
# (ad, görünen ad, girdi sınırı, çıktı sınırı, düşünme)
MODELS = [("gemini-2.5-pro", "Gemini 2.5 Pro", 1048576, 65536, True),
          ("gemini-2.5-flash", "Gemini 2.5 Flash", 1048576, 65536, True),
          ("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite", 1048576, 65536, True),
          ("gemini-2.0-flash", "Gemini 2.0 Flash", 1048576, 8192, False)]
EMBED = "gemini-embedding-001"
GEN = ["generateContent", "countTokens", "createCachedContent", "batchGenerateContent"]


def gerr(code: int, status: str, msg: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": msg, "status": status}}, code)


def _card(name, disp, inp, out, think) -> dict:
    c = {"name": f"models/{name}", "version": name.split("-")[1] if name.count("-") else "001", "displayName": disp,
         "description": f"Stable version of {disp}", "inputTokenLimit": inp, "outputTokenLimit": out,
         "supportedGenerationMethods": GEN, "temperature": 1, "topP": 0.95, "topK": 64, "maxTemperature": 2}
    if think:
        c["thinking"] = True
    return c


def _key(request: Request):
    """Anahtarsız istek Google'daki gibi 403; anahtar ne olursa olsun kabul (relay)."""
    request.state.tags.add("gemini_api")
    if api_key(request):
        return None
    return gerr(403, "PERMISSION_DENIED", "Method doesn't allow unregistered callers (callers without established "
                "identity). Please use API Key or other form of API consumer identity to call this API.")


def _parts_text(parts) -> str:
    return "\n".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and "text" in p) \
        if isinstance(parts, list) else ""


def to_messages(p: dict) -> list[dict]:
    """contents/systemInstruction → normalize() biçimi (functionCall → tool_calls, functionResponse → tool)."""
    out = []
    si = p.get("systemInstruction") or p.get("system_instruction")
    system = si if isinstance(si, str) else _parts_text(si.get("parts")) if isinstance(si, dict) else ""
    if system:
        out.append({"role": "system", "content": system})
    for c in p.get("contents") if isinstance(p.get("contents"), list) else []:
        if not isinstance(c, dict):
            continue
        parts = [x for x in c.get("parts") or [] if isinstance(x, dict)]
        text = _parts_text(parts)
        if c.get("role") == "model":
            n = {"role": "assistant", "content": text}
            calls = [{"name": str(x["functionCall"].get("name", "")),
                      "arguments": x["functionCall"].get("args") if isinstance(x["functionCall"].get("args"), dict) else {}}
                     for x in parts if isinstance(x.get("functionCall"), dict)]
            if calls:
                n["tool_calls"] = calls
            out.append(n)
            continue
        res = [x["functionResponse"] for x in parts if isinstance(x.get("functionResponse"), dict)]
        out += [{"role": "tool", "content": json.dumps(r.get("response"), ensure_ascii=False, default=str),
                 "tool_call_id": str(r.get("name", ""))} for r in res]
        if text or not res:
            out.append({"role": "user", "content": text})
    return out


def to_tools(tools) -> list[dict]:
    out = []
    for t in tools if isinstance(tools, list) else []:
        for f in (t.get("functionDeclarations") or t.get("function_declarations") or []) if isinstance(t, dict) else []:
            if isinstance(f, dict) and f.get("name"):
                out.append({"type": "function", "function": {"name": f["name"], "description": str(f.get("description", "")),
                                                             "parameters": f.get("parameters") or {"type": "object"}}})
    return out


def _model(raw: str) -> tuple[str, str]:
    """'gemini-2.5-pro:generateContent' → (model, method)."""
    name, _, method = raw.removeprefix("models/").rpartition(":")
    return (name, method) if name else (method, "")


@router.get("/models")
async def list_models(request: Request):
    if deny := _key(request):
        return deny
    request.state.tags.add("model_enum")
    emb = {"name": f"models/{EMBED}", "version": "001", "displayName": "Gemini Embedding 001",
           "description": "Obtain a distributed representation of a text.", "inputTokenLimit": 2048,
           "outputTokenLimit": 1, "supportedGenerationMethods": ["embedContent", "countTextTokens", "countTokens"]}
    return {"models": [_card(*m) for m in MODELS] + [emb]}


@router.get("/models/{name}")
async def get_model(name: str, request: Request):
    if deny := _key(request):
        return deny
    request.state.tags.add("model_enum")
    hit = next((m for m in MODELS if m[0] == name), None)
    return _card(*hit) if hit else gerr(404, "NOT_FOUND", f"models/{name} is not found for API version v1beta, or is not "
                                        "supported for generateContent. Call ListModels to see the list of available "
                                        "models and their supported methods.")


@router.post("/models/{raw}")
async def call(raw: str, request: Request):
    if deny := _key(request):
        return deny
    model, method = _model(raw)
    st = request.state
    try:
        p = await request.json()
    except Exception:
        p = None
    if not isinstance(p, dict):
        return gerr(400, "INVALID_ARGUMENT", "Invalid JSON payload received.")
    st.extra.update(requested_model=model, api_key=api_key(request)[:200])
    if method in ("embedContent", "batchEmbedContents"):
        reqs = p.get("requests") if method == "batchEmbedContents" else [p]
        texts = [_parts_text((r.get("content") or {}).get("parts")) for r in reqs or [] if isinstance(r, dict)][:64]
        st.tags.add("llm_usage")
        vecs = [{"values": embedding(t, 3072)} for t in texts]
        return {"embeddings": vecs} if method == "batchEmbedContents" else {"embedding": vecs[0] if vecs else {"values": []}}
    if method not in ("generateContent", "streamGenerateContent", "countTokens"):
        return gerr(404, "NOT_FOUND", f"Method not found: {method}")
    if not model.startswith(("gemini", "gemma")):
        return gerr(404, "NOT_FOUND", f"models/{model} is not found for API version v1beta, or is not supported for "
                    f"{method}. Call ListModels to see the list of available models and their supported methods.")
    msgs = to_messages(p)
    pt = sum(ntok(m["content"]) for m in msgs) + 4
    if method == "countTokens":
        return {"totalTokens": pt, "promptTokensDetails": [{"modality": "TEXT", "tokenCount": pt}]}
    if not any(m["role"] != "system" for m in msgs):
        return gerr(400, "INVALID_ARGUMENT", "* GenerateContentRequest.contents: contents is not specified\n")
    if any(m["role"] == "system" for m in msgs):
        st.tags.add("custom_system_prompt")
    cfg = p.get("generationConfig") if isinstance(p.get("generationConfig"), dict) else {}
    msg, tags = await agent.run_raw(st.sid, msgs, model=model, tools=to_tools(p.get("tools")),
                                    max_tokens=cfg.get("maxOutputTokens"), temperature=cfg.get("temperature"))
    st.tags |= tags | {"llm_usage"}
    if msg is None:
        return gerr(503, "UNAVAILABLE", "The model is overloaded. Please try again later.")
    text, calls = msg["content"], msg.get("tool_calls") or []
    parts = ([{"text": text}] if text else []) + [{"functionCall": {"name": c["name"], "args": c["arguments"]}}
                                                  for c in calls]
    ct = ntok(text) + 12 * len(calls)
    usage = {"promptTokenCount": pt, "candidatesTokenCount": ct, "totalTokenCount": pt + ct,
             "promptTokensDetails": [{"modality": "TEXT", "tokenCount": pt}]}
    if "2.5" in model:  # düşünen modellerde düşünme token'ı da sayılır
        th = 40 + len(text) // 3
        usage.update(totalTokenCount=pt + ct + th, thoughtsTokenCount=th)
    body = {"candidates": [{"content": {"parts": parts, "role": "model"}, "finishReason": "STOP", "index": 0}],
            "usageMetadata": usage, "modelVersion": model,
            "responseId": base64.urlsafe_b64encode(os.urandom(15)).decode()[:20]}
    if method == "streamGenerateContent":  # alt=sse → SSE, yoksa JSON dizi
        if request.query_params.get("alt") == "sse":
            async def gen():
                yield "data: " + dumps(body) + "\r\n\r\n"
            return StreamingResponse(gen(), media_type="text/event-stream")
        return JSONResponse([body])
    return body
