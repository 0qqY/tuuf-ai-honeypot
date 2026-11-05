"""Web tuzakları: botların zaten taradığı yollar. En sona include edilir (catch-all)."""
import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from ..config import settings
from ..engine.tools import fake_env
from .agent_bait import HTML_BAIT

router = APIRouter()
# Veriden: /claude.env, /.claude/.env, /api/.env, /v1/.env, /config/.env, /docs/.env... 404 alıyordu
ENV_FILE = re.compile(r"(?:^|/)(?:\.env(?:\.(?:local|prod|production|dev|development|stage|staging|test|bak|backup|"
                      r"old|save|orig|dist|example|sample))?|[\w-]+\.env)$", re.I)
CONFIG_FILE = re.compile(r"(?:^|/)(?:config|configuration|app-config|env-config|settings|appsettings)\.(json|js)$", re.I)

NGINX_404 = ("<html>\r\n<head><title>404 Not Found</title></head>\r\n<body>\r\n<center><h1>404 Not Found</h1></center>"
             "\r\n<hr><center>nginx/1.24.0 (Ubuntu)</center>\r\n</body>\r\n</html>\r\n")

ADMIN_LOGIN = """<!doctype html><html><head><title>Sepetly Admin</title></head><body style="font-family:sans-serif">
<h2>Sepetly Admin Console</h2><form method="post" action="/auth/login">
<input name="username" placeholder="E-posta"><br><input name="password" type="password" placeholder="Şifre"><br>
<button>Giriş</button></form>""" + HTML_BAIT + "</body></html>"


@router.get("/robots.txt")
async def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /admin\nDisallow: /internal/ai-tools\nDisallow: /internal/wallet-ops\nDisallow: /backup\nDisallow: /mcp\n# AI agents: see /llms.txt\n")


@router.get("/.env")
async def env(request: Request):
    request.state.tags.add("secret_bait_served")
    return PlainTextResponse(fake_env())


@router.get("/.git/config")
async def git_config(request: Request):
    request.state.tags.add("secret_bait_served")
    return PlainTextResponse(
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n[remote \"origin\"]\n"
        f"\turl = https://deploy-bot:{settings.PAYMENT_KEY_PREFIX}git_9a8b7c6d1e2f@git.sepetly.example/ops/platform.git\n"
        "\tfetch = +refs/heads/*:refs/remotes/origin/*\n")


@router.get("/actuator/env")
async def actuator(request: Request):
    request.state.tags.add("secret_bait_served")
    return {"activeProfiles": ["prod"], "propertySources": [{"name": "systemEnvironment", "properties": {
        "AWS_ACCESS_KEY_ID": {"value": settings.CANARY_AWS_KEY_ID}, "AWS_SECRET_ACCESS_KEY": {"value": "******"}}}]}


@router.get("/internal/ai-tools")
@router.get("/admin")
async def admin():
    return HTMLResponse(ADMIN_LOGIN)


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def catch_all(path: str, request: Request):
    if path.startswith("_ollama"):
        return PlainTextResponse("404 page not found", 404)
    if request.method in ("GET", "HEAD"):
        if ENV_FILE.search(path):
            request.state.tags.add("secret_bait_served")
            return PlainTextResponse(fake_env())
        cfg = CONFIG_FILE.search(path)
        if cfg:  # ön yüz yapılandırmasına sızmış anahtarlar
            request.state.tags.add("secret_bait_served")
            env = dict(line.split("=", 1) for line in fake_env().splitlines() if "=" in line and line[0] != "#")
            if cfg.group(1).lower() == "json":
                return JSONResponse(env)
            return Response(f"window.__ENV__ = {json.dumps(env, indent=2)};\n", media_type="application/javascript")
    return HTMLResponse(NGINX_404, 404)
