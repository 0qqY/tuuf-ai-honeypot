import hashlib
import hmac
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from . import db
from .config import settings
from .detect import rules
from .routers import (agent_bait, anthropic_compat, chat_ui, exchange, gemini_compat, llm_servers, mcp,
                      ollama_compat, openai_compat, traps)

DROP_HEADERS = {"cookie", "x-redteam"}  # x-redteam: test anahtarı DB'ye yazılmasın
SID_RE = re.compile(r"[0-9a-f]{16}")
# Caddy her LLM portunu önekle yollar (:11434 → /_ollama, :8000 → /_vllm ...): önek = yüzey
PORT_SURFACES = {"_ollama": "ollama", "_vllm": "vllm", "_lmstudio": "lmstudio", "_litellm": "litellm",
                 "_llamacpp": "llamacpp", "_openwebui": "openwebui", "_gradio": "gradio",
                 "_btcrpc": "btc_rpc"}


@asynccontextmanager
async def lifespan(_):
    await db.init()
    yield
    await db.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def capture(request: Request, call_next):
    raw = await request.body()
    body = raw[: settings.MAX_BODY_LOG].decode("utf-8", "replace")
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "0.0.0.0")
    path = request.url.path
    head = path.split("/", 2)[1]
    if head in PORT_SURFACES:
        surface, log_path = PORT_SURFACES[head], path[len(head) + 1:] or "/"
    elif path.startswith(("/v1/", "/v1beta/")):  # relay: OpenAI, Anthropic (/v1/messages), Gemini (/v1beta)
        surface, log_path = "openai_api", path
    elif path.startswith(exchange.WOPS):
        surface, log_path = "wallet_ops", path
    elif path in ("/mcp", "/sse", "/messages"):
        surface, log_path = "mcp", path
    else:
        surface, log_path = "web", path
    ua = request.headers.get("user-agent", "")
    sid = request.cookies.get("sid", "")
    if not SID_RE.fullmatch(sid):  # çerez saldırgan kontrolünde: panelde XSS'e ve oturum karıştırmaya kapı açmasın
        sid = hashlib.sha1(f"{ip}|{ua}|{request.headers.get('authorization', '')}".encode()).hexdigest()[:16]

    st = request.state
    st.sid, st.extra = sid, {}
    hay = f"{log_path}?{request.url.query}\n{body}\n" + "\n".join(f"{k}: {v}" for k, v in request.headers.items())
    st.tags = rules.scan_web(hay) | rules.scan_canary(hay)
    if settings.REDTEAM_TOKEN and hmac.compare_digest(request.headers.get("x-redteam", "").encode(),
                                                      settings.REDTEAM_TOKEN.encode()):
        st.tags.add("redteam")  # kendi DeepSeek testimiz (redteam/): kaydedilir, saldırgan istatistiğine girmez

    status = 500
    try:
        resp = await call_next(request)
        status = resp.status_code
    except Exception:
        resp = HTMLResponse("<h1>502 Bad Gateway</h1>", 502)
        status = 502
    if surface in ("web", "wallet_ops"):
        resp.headers["server"] = "nginx/1.24.0 (Ubuntu)"
        if request.cookies.get("sid") != sid:
            resp.set_cookie("sid", sid, httponly=True, samesite="lax")

    await db.log_event(
        session_id=sid, src_ip=ip, surface=surface, method=request.method, path=log_path, status=status, ua=ua,
        headers={k: v for k, v in request.headers.items() if k not in DROP_HEADERS},
        body=body, tags=st.tags, severity=rules.severity(st.tags), extra={"query": request.url.query, **st.extra},
    )
    return resp


app.include_router(openai_compat.router)
app.include_router(anthropic_compat.router)
app.include_router(gemini_compat.router)
app.include_router(ollama_compat.router)
app.include_router(ollama_compat.v1_router)
for r in llm_servers.ROUTERS:  # sıra önemli: ürünün kendi uçları → OpenAI uyumlu uçlar → ürünün 404'ü
    app.include_router(r)
app.include_router(mcp.router)
app.include_router(agent_bait.router)
app.include_router(chat_ui.router)
for r in exchange.ROUTERS:
    app.include_router(r)
app.include_router(traps.router)  # catch-all en sonda
