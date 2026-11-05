"""Anthropic Messages API (/v1/messages) taklidi: 443'teki relay'de ve LiteLLM'de (:4000). .env yemindeki
ANTHROPIC_BASE_URL buraya döner; istek normalize() biçimine çevrilip ham moda (agent.run_raw) gider."""
import json
import random
import string

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..engine import agent
from .openai_compat import api_key, dumps, ntok, pieces

# (kimlik, görünen ad, çıkış tarihi) — en yeni önce, /v1/models sırası
MODELS = [("claude-sonnet-4-5-20250929", "Claude Sonnet 4.5", "2025-09-29T00:00:00Z"),
          ("claude-opus-4-1-20250805", "Claude Opus 4.1", "2025-08-05T00:00:00Z"),
          ("claude-opus-4-20250514", "Claude Opus 4", "2025-05-22T00:00:00Z"),
          ("claude-sonnet-4-20250514", "Claude Sonnet 4", "2025-05-22T00:00:00Z"),
          ("claude-3-7-sonnet-20250219", "Claude Sonnet 3.7", "2025-02-24T00:00:00Z"),
          ("claude-3-5-haiku-20241022", "Claude Haiku 3.5", "2024-10-22T00:00:00Z")]
ORG = "7c1e4b2a-93d5-4f08-b6e1-2a9f0c3d5e71"
B62 = string.ascii_letters + string.digits


def _rid(prefix: str, n: int) -> str:
    return prefix + "".join(random.choices(B62, k=n))


def _hdrs():
    return {"request-id": _rid("req_011C", 20), "anthropic-organization-id": ORG, "x-proxy": "spt-relay/1.4"}


def err(kind: str, msg: str, code: int) -> JSONResponse:
    return JSONResponse({"type": "error", "error": {"type": kind, "message": msg},
                         "request_id": _rid("req_011C", 20)}, code, headers=_hdrs())


def is_anthropic(request: Request) -> bool:
    return "anthropic-version" in request.headers or "x-api-key" in request.headers


def models_alt(request: Request, model: str | None = None):
    """Relay'in /v1/models'ı: Anthropic SDK'sıyla gelene Anthropic biçimi, diğerlerine None (OpenAI biçimi)."""
    if not is_anthropic(request):
        return None
    if not api_key(request):
        return err("authentication_error", "x-api-key header is required", 401)
    cards = [{"type": "model", "id": i, "display_name": d, "created_at": t} for i, d, t in MODELS]
    if model is not None:
        hit = next((c for c in cards if c["id"] == model), None)
        return JSONResponse(hit, headers=_hdrs()) if hit else err("not_found_error", f"model: {model}", 404)
    return JSONResponse({"data": cards, "has_more": False, "first_id": cards[0]["id"], "last_id": cards[-1]["id"]},
                        headers=_hdrs())


def _blocks(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _text(content) -> str:
    return "\n".join(str(b.get("text", "")) for b in _blocks(content) if b.get("type") == "text")


def to_messages(payload: dict) -> list[dict]:
    """Anthropic istek gövdesi → normalize() biçimi (system, user, assistant+tool_calls, tool)."""
    out = []
    system = _text(payload.get("system"))
    if system:
        out.append({"role": "system", "content": system})
    for m in payload.get("messages") if isinstance(payload.get("messages"), list) else []:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        blocks, text = _blocks(m.get("content")), _text(m.get("content"))
        if m["role"] == "assistant":
            n = {"role": "assistant", "content": text}
            calls = [{"name": str(b.get("name", "")), "arguments": b["input"] if isinstance(b.get("input"), dict) else {}}
                     for b in blocks if b.get("type") == "tool_use"]
            if calls:
                n["tool_calls"] = calls
            out.append(n)
            continue
        results = [b for b in blocks if b.get("type") == "tool_result"]
        out += [{"role": "tool", "content": _text(b.get("content")), "tool_call_id": str(b.get("tool_use_id", ""))}
                for b in results]
        if text or not results:
            out.append({"role": "user", "content": text})
    return out


def to_tools(tools) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": str(t.get("description", "")),
                                              "parameters": t.get("input_schema") or {"type": "object"}}}
            for t in tools if isinstance(t, dict) and t.get("name")] if isinstance(tools, list) else []


def make_router(prefix: str, *, gate=None, headers=_hdrs) -> APIRouter:
    """POST {prefix}/messages (+ count_tokens). gate: yetki kontrolü (LiteLLM), yoksa herhangi bir anahtar yeter."""
    r = APIRouter(prefix=prefix)

    async def parse(request: Request):
        request.state.tags.add("anthropic_api")
        if gate and (deny := gate(request)):
            return None, deny
        if not gate and not api_key(request):
            return None, err("authentication_error", "x-api-key header is required", 401)
        try:
            p = await request.json()
        except Exception:
            p = None
        if not isinstance(p, dict):
            return None, err("invalid_request_error", "There was an error parsing the body of your request.", 400)
        return p, None

    @r.post("/messages/count_tokens")
    async def count_tokens(request: Request):
        p, deny = await parse(request)
        if deny:
            return deny
        return JSONResponse({"input_tokens": sum(ntok(m["content"]) for m in to_messages(p)) + 8}, headers=headers())

    @r.post("/messages")
    async def messages(request: Request):
        p, deny = await parse(request)
        if deny:
            return deny
        for field in ("model", "max_tokens", "messages"):
            if field not in p:
                return err("invalid_request_error", f"{field}: Field required", 400)
        model, msgs = str(p["model"]), to_messages(p)
        st = request.state
        st.extra.update(requested_model=model, max_tokens=p.get("max_tokens"), api_key=api_key(request)[:200])
        if not model.startswith("claude"):
            return err("not_found_error", f"model: {model}", 404)
        if not any(m["role"] != "system" for m in msgs):
            return err("invalid_request_error", "messages: at least one message is required", 400)
        if any(m["role"] == "system" for m in msgs):
            st.tags.add("custom_system_prompt")
        msg, tags = await agent.run_raw(st.sid, msgs, model=model, tools=to_tools(p.get("tools")),
                                        max_tokens=p.get("max_tokens"), temperature=p.get("temperature"))
        st.tags |= tags | {"llm_usage"}
        if msg is None:
            return err("overloaded_error", "Overloaded", 529)
        text, calls = msg["content"], msg.get("tool_calls") or []
        uses = [{"type": "tool_use", "id": _rid("toolu_01", 22), "name": c["name"], "input": c["arguments"]}
                for c in calls]
        stop = "tool_use" if uses else "end_turn"
        pt, ct = sum(ntok(m["content"]) for m in msgs) + 8, ntok(text) + 12 * len(uses)
        usage = {"input_tokens": pt, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                 "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
                 "output_tokens": ct, "service_tier": "standard"}
        base = {"id": _rid("msg_01", 22), "type": "message", "role": "assistant", "model": model}

        if p.get("stream"):
            def ev(name, data, raw=None):
                return f"event: {name}\ndata: {raw or dumps({'type': name, **data})}\n\n"

            async def gen():
                yield ev("message_start", {"message": {**base, "content": [], "stop_reason": None, "stop_sequence": None,
                                                       "usage": {**usage, "output_tokens": 1}}})
                i = 0
                if text:
                    yield ev("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}})
                    yield ev("ping", {}, raw='{"type": "ping"}')
                    for piece in pieces(text):
                        yield ev("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": piece}})
                    yield ev("content_block_stop", {"index": 0})
                    i = 1
                for j, u in enumerate(uses, i):
                    yield ev("content_block_start", {"index": j, "content_block": {**u, "input": {}}})
                    for part in ("", json.dumps(u["input"], ensure_ascii=False)):
                        yield ev("content_block_delta", {"index": j, "delta": {"type": "input_json_delta",
                                                                               "partial_json": part}})
                    yield ev("content_block_stop", {"index": j})
                yield ev("message_delta", {"delta": {"stop_reason": stop, "stop_sequence": None},
                                           "usage": {"output_tokens": ct}})
                yield ev("message_stop", {})
            return StreamingResponse(gen(), media_type="text/event-stream", headers=headers())

        content = ([{"type": "text", "text": text}] if text else []) + uses
        return JSONResponse({**base, "content": content, "stop_reason": stop, "stop_sequence": None, "usage": usage},
                            headers=headers())

    return r


router = make_router("/v1")
