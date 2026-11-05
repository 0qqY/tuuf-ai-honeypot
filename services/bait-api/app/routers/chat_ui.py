from collections import defaultdict, deque
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..engine import agent
from .agent_bait import HTML_BAIT

router = APIRouter()
STATIC = Path(__file__).resolve().parent.parent / "static"
HISTORY: dict[str, deque] = defaultdict(lambda: deque(maxlen=12))  # sid -> son 12 mesaj


INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8").replace("</body>", HTML_BAIT + "</body>")


@router.get("/")
async def index():
    return HTMLResponse(INDEX_HTML)


@router.post("/auth/login")
async def login(request: Request):
    ct = request.headers.get("content-type", "")
    data = await request.json() if "json" in ct else dict(await request.form())
    user, pw = str(data.get("username", ""))[:200], str(data.get("password", ""))[:200]
    request.state.tags.add("credential_capture")
    request.state.extra.update(username=user, password=pw)
    return {"ok": True, "user": {"name": user or "ops", "role": "support_agent", "team": "Merchant Ops"}}


@router.post("/api/assistant/chat")
async def chat(request: Request):
    try:
        msg = str((await request.json()).get("message", ""))[:8000]
    except Exception:
        return JSONResponse({"error": "bad request"}, 400)
    if not msg.strip():
        return JSONResponse({"error": "empty message"}, 400)
    sid = request.state.sid
    HISTORY[sid].append({"role": "user", "content": msg})
    reply, tags = await agent.run(sid, list(HISTORY[sid]))
    HISTORY[sid].append({"role": "assistant", "content": reply})
    request.state.tags |= tags
    return {"reply": reply}
