"""Gözlem paneli API'si. Yalnızca VPN (Tailscale) üzerinden erişilmeli + ADMIN_TOKEN."""
import asyncio
import hmac
import json
import math
import os
import re
from datetime import timedelta
from functools import lru_cache
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket
from fastapi.responses import FileResponse, PlainTextResponse

DSN = os.getenv("DATABASE_URL", "postgresql://hp:hp@db:5432/hp")
TOKEN = os.getenv("ADMIN_TOKEN", "")
pool: asyncpg.Pool | None = None
# Ülke çözümleme: derlemede indirilen DB-IP Lite (fetch_assets.py); yoksa ülke sekmesi "??" gösterir
try:
    import maxminddb
    GEO = maxminddb.open_database(os.getenv("GEOIP_DB", "/srv/geo/country.mmdb"))
except Exception:
    GEO = None
FLAGS = Path(os.getenv("FLAGS_DIR", "/srv/flags"))


@asynccontextmanager
async def lifespan(_):
    global pool
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=5)
    yield
    await pool.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


def _ok(t: str | None) -> bool:
    return bool(TOKEN) and t is not None and hmac.compare_digest(t, TOKEN)


def auth(x_admin_token: str | None = Header(None)):
    if not _ok(x_admin_token):
        raise HTTPException(401)


def _row(r):
    d = dict(r)
    for k in ("ts",):
        if d.get(k):
            d[k] = d[k].isoformat()
    if d.get("src_ip") is not None:
        d["src_ip"] = str(d["src_ip"])
        d["cc"], d["country"] = _country(d["src_ip"])
    for k in ("headers", "extra"):
        if isinstance(d.get(k), str):
            d[k] = json.loads(d[k])
    return d


# Gürültü: favicon/logo/varlık ve kök sayfa GET'leri, saldırı ya da yem etiketi yoksa (yalnız scanner_ua olabilir).
# DB'ye yazılmaz, sorguda hesaplanır: eski kayıtlar da sınıflanır, kural değişince veri değişmez
NOISE = ("(surface = 'web' AND method IN ('GET','HEAD') AND tags <@ ARRAY['scanner_ua']::text[] AND "
         "(path = '/' OR path ~* '\\.(ico|png|jpe?g|gif|svg|webp|bmp|css|woff2?|ttf|eot|map)$'))")
EV_COLS = f"id,ts,session_id,src_ip,surface,method,path,status,ua,tags,severity,extra,{NOISE} AS noise"


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/bee.webp")
async def bee_img():
    return FileResponse(Path(__file__).parent / "static" / "bee.webp", media_type="image/webp",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/flags/{cc}.svg")
async def flag(cc: str):
    f = FLAGS / f"{cc}.svg"
    if not re.fullmatch(r"[a-z]{2}", cc) or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=604800"})


@lru_cache(maxsize=65536)
def _country(ip: str) -> tuple[str, str]:
    try:
        c = (GEO.get(ip) or {}).get("country") or {}
    except Exception:
        c = {}
    return c.get("iso_code") or "??", (c.get("names") or {}).get("en") or "Unknown"


@app.get("/api/stats", dependencies=[Depends(auth)])
async def stats(hours: int = 24, hide_noise: bool = False):
    iv = timedelta(hours=int(hours))  # asyncpg interval için timedelta ister
    w = "ts > now()-$1::interval" + (f" AND NOT {NOISE}" if hide_noise else "")
    async with pool.acquire() as c:
        q = lambda sql: c.fetch(sql, iv)  # noqa: E731
        totals = await c.fetchrow(
            "SELECT count(*) events, count(DISTINCT src_ip) ips, count(DISTINCT session_id) sessions, "
            "count(*) FILTER (WHERE severity>=2) high, count(*) FILTER (WHERE 'canary_reuse'=ANY(tags)) canary, "
            "count(DISTINCT src_ip) FILTER (WHERE severity>=2) engaged, "  # taramanın ötesine geçen IP
            "count(*) FILTER (WHERE severity>=3 AND ts > now()-interval '1 hour') crit_1h "
            f"FROM events WHERE {w}", iv)
        noise = await c.fetchval(f"SELECT count(*) FROM events WHERE ts > now()-$1::interval AND {NOISE}", iv)
        return {
            "totals": {**dict(totals), "noise": noise},
            "by_surface": [dict(r) for r in await q(
                "SELECT surface, count(*) n, count(DISTINCT src_ip) ips, count(*) FILTER (WHERE severity>=2) high, "
                "count(*) FILTER (WHERE ts > now()-interval '1 hour') last_hour "
                f"FROM events WHERE {w} GROUP BY 1 ORDER BY 2 DESC")],
            "by_tag": [dict(r) for r in await q(
                f"SELECT t tag, count(*) n FROM events, unnest(tags) t WHERE {w} GROUP BY 1 ORDER BY 2 DESC LIMIT 20")],
            "top_ips": [{"ip": str(r["src_ip"]), "cc": _country(str(r["src_ip"]))[0], "n": r["n"], "max_sev": r["s"]} for r in await q(
                f"SELECT src_ip, count(*) n, max(severity) s FROM events WHERE {w} GROUP BY 1 ORDER BY 2 DESC LIMIT 15")],
            "top_paths": [dict(r) for r in await q(
                f"SELECT path, count(*) n FROM events WHERE {w} GROUP BY 1 ORDER BY 2 DESC LIMIT 15")],
            "hourly": [{"h": r["h"].isoformat(), "n": r["n"], "high": r["high"]} for r in await q(
                "SELECT date_trunc('hour',ts) h, count(*) n, count(*) FILTER (WHERE severity>=2) high "
                f"FROM events WHERE {w} GROUP BY 1 ORDER BY 1")],
        }


# --- Saldırı puanı: bir "saldırı" = bir kaynak IP'nin pencere içindeki tüm etkinliği (gürültü ve kendi IP'lerimiz hariç).
# Kendi red-team testimizin (redteam etiketi) olayları da kendi IP'lerimiz gibi sayılmaz.
# Etiket önemleri bait-api detect/rules.py SEVERITY ile aynı tutulmalı; listede olmayan etiket 1 sayılır
OWN_IPS = [x for x in os.getenv("OWN_IPS", "").split(",") if x.strip()]
OURS = "(src_ip = ANY($2::text[]::inet[]) OR 'redteam' = ANY(tags))"
TAG_SEV = {
    "agent_bait_followed": 3, "github_bait_reuse": 3, "llm_key_reuse": 3, "mcp_tool_call": 3, "canary_reuse": 3,
    "cmd_injection": 3, "log4shell": 3, "tool_abuse": 3, "model_tamper": 3, "malicious_pull": 3, "function_upload": 3,
    "owu_config_key_reuse": 3, "mcp_enum": 2, "mcp_init": 2, "sqli": 2, "ssrf": 2, "path_traversal": 2, "data_exfil": 2,
    "prompt_injection": 2, "system_prompt_extraction": 2, "jailbreak": 2, "credential_capture": 2,
    "encoding_evasion": 2, "xss": 2, "harmful_request": 2, "default_credential": 2, "account_signup": 2,
    "key_generate": 2, "owu_config_read": 2, "scanner_ua": 0,
    # borsa yemi (wallet-ops API + bitcoind RPC)
    "wallet_ops_key_reuse": 3, "btc_rpc_cred_reuse": 3, "withdrawal_submit": 3, "whitelist_change": 3,
    "privkey_dump": 3, "wallet_unlock": 2, "devops_leak_read": 1, "wallet_api_probe": 1, "btc_rpc_probe": 1,
    "btc_rpc_call": 1,
}
TAG_PTS = {3: 30, 2: 10, 1: 3, 0: 0}
LEVELS = ((75, "critical"), (50, "high"), (25, "medium"), (0, "low"))


def _score(tags: dict, n_surf: int, high: int, hours: int) -> tuple[int, dict]:
    """0–100. Ham puan = etki (farklı etiketlerin önem puanı) + yayılım (fazladan yüzey) + hacim (orta+ olay)
    + süreklilik (etkin saat); 100·(1−e^(−ham/70)) ile doyurulur: tek bir kritik etiket ~35, zincir kuran ~80+."""
    b = {"impact": sum(TAG_PTS[TAG_SEV.get(t, 1)] for t in tags),
         "breadth": min(20, 4 * (n_surf - 1)),
         "volume": round(min(15, 4 * math.log1p(high)), 1),
         "persistence": round(min(10, 1.5 * hours), 1)}
    return round(100 * (1 - math.exp(-sum(b.values()) / 70))), b


async def _attacks(c, hours: int, limit: int) -> list[dict]:
    iv = timedelta(hours=hours)
    w = f"ts > now()-$1::interval AND NOT {NOISE} AND NOT {OURS}"
    rows = await c.fetch(
        "SELECT src_ip, count(*) n, count(*) FILTER (WHERE severity>=2) high, max(severity) maxsev, "
        "array_agg(DISTINCT surface) surfaces, count(DISTINCT date_trunc('hour', ts)) hours, min(ts) first, "
        f"max(ts) last, left(mode() WITHIN GROUP (ORDER BY ua), 80) ua FROM events WHERE {w} GROUP BY 1 HAVING max(severity) >= 1", iv, OWN_IPS)
    tags: dict[str, dict] = {}
    for r in await c.fetch(f"SELECT src_ip, t, count(*) n FROM events, unnest(tags) t WHERE {w} GROUP BY 1, 2",
                           iv, OWN_IPS):
        tags.setdefault(str(r["src_ip"]), {})[r["t"]] = r["n"]
    out = []
    for r in rows:
        ip = str(r["src_ip"])
        tg = tags.get(ip, {})
        score, b = _score(tg, len(r["surfaces"]), r["high"], r["hours"])
        tl = sorted(({"tag": t, "n": n, "sev": TAG_SEV.get(t, 1)} for t, n in tg.items()),
                    key=lambda x: (-x["sev"], -x["n"]))
        cc, cname = _country(ip)
        out.append({"ip": ip, "cc": cc, "country": cname, "score": score, "level": next(name for lim, name in LEVELS if score >= lim),
                    "breakdown": b, "tags": tl, "lead": next((x["tag"] for x in tl if x["sev"]), None),
                    "surfaces": sorted(r["surfaces"]), "n": r["n"], "high": r["high"], "max_sev": r["maxsev"],
                    "hours": r["hours"], "first": r["first"].isoformat(), "last": r["last"].isoformat(),
                    "ua": r["ua"]})
    out.sort(key=lambda a: (-a["score"], -a["high"], -a["n"]))
    return out[:limit]


@app.get("/api/countries", dependencies=[Depends(auth)])
async def countries(hours: int = 24, limit: int = Query(15, ge=1, le=100)):
    """Saldırı kaynağı ülkeler (gürültü ve kendi IP'lerimiz hariç). Sıra: orta+ önemli olay, sonra toplam olay."""
    iv = timedelta(hours=int(hours))
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT src_ip, count(*) n, count(*) FILTER (WHERE severity>=2) high, max(severity) s FROM events "
            f"WHERE ts > now()-$1::interval AND NOT {NOISE} AND NOT {OURS} GROUP BY 1",
            iv, OWN_IPS)
    agg: dict[str, dict] = {}
    for r in rows:
        ip = str(r["src_ip"])
        cc, name = _country(ip)
        a = agg.setdefault(cc, {"cc": cc, "name": name, "n": 0, "high": 0, "ips": 0, "attackers": 0, "max_sev": 0,
                                "top": []})
        a["n"] += r["n"]
        a["high"] += r["high"]
        a["ips"] += 1
        a["attackers"] += r["s"] >= 2  # taramanın ötesine geçen IP
        a["max_sev"] = max(a["max_sev"], r["s"])
        a["top"].append({"ip": ip, "n": r["n"], "high": r["high"], "max_sev": r["s"]})
    out = sorted(agg.values(), key=lambda a: (-a["high"], -a["n"]))[:limit]
    for a in out:
        a["top"] = sorted(a["top"], key=lambda x: (-x["high"], -x["n"]))[:8]
    return {"geo": GEO is not None, "ips": len(rows), "countries": out}


@app.get("/api/attacks", dependencies=[Depends(auth)])
async def attacks(limit: int = Query(5, ge=1, le=50)):
    """Ana sayfa raporu: son 24 saatin ve son 7 günün en yüksek puanlı saldırıları."""
    async with pool.acquire() as c:
        return {"daily": await _attacks(c, 24, limit), "weekly": await _attacks(c, 168, limit)}


@app.get("/api/events", dependencies=[Depends(auth)])
async def events(limit: int = Query(100, le=500), tag: str | None = None, ip: str | None = None,
                 surface: str | None = None, min_sev: int = 0, before_id: int | None = None, hide_noise: bool = False):
    where, args = ["severity >= $1"] + ([f"NOT {NOISE}"] if hide_noise else []), [min_sev]
    for cond, val in (("$%d = ANY(tags)", tag), ("src_ip = $%d::text::inet", ip), ("surface = $%d", surface),
                      ("id < $%d", before_id)):
        if val is not None:
            args.append(val)
            where.append(cond % len(args))
    args.append(limit)
    async with pool.acquire() as c:
        rows = await c.fetch(f"SELECT {EV_COLS} FROM events WHERE {' AND '.join(where)} "
                             f"ORDER BY id DESC LIMIT ${len(args)}", *args)
    return [_row(r) for r in rows]


@app.get("/api/events/{eid}", dependencies=[Depends(auth)])
async def event(eid: int):
    async with pool.acquire() as c:
        r = await c.fetchrow("SELECT * FROM events WHERE id=$1", eid)
    if not r:
        raise HTTPException(404)
    return _row(r)


@app.get("/api/sessions", dependencies=[Depends(auth)])
async def sessions(limit: int = Query(50, le=200)):
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT m.session_id, min(m.ts) first, max(m.ts) last, count(DISTINCT m.id) n, "
            "array_remove(array_agg(DISTINCT t), NULL) tags "
            "FROM messages m LEFT JOIN LATERAL unnest(m.tags) t ON true "
            "GROUP BY m.session_id ORDER BY max(m.ts) DESC LIMIT $1", limit)
    return [{"session_id": r["session_id"], "first": r["first"].isoformat(), "last": r["last"].isoformat(),
             "n": r["n"], "tags": [t for t in r["tags"] if t]} for r in rows]


@app.get("/api/sessions/{sid}", dependencies=[Depends(auth)])
async def session(sid: str):
    async with pool.acquire() as c:
        msgs = await c.fetch("SELECT id,ts,role,content,tool_name,tags,reviewed FROM messages "
                             "WHERE session_id=$1 ORDER BY id", sid)
        evs = await c.fetch(f"SELECT {EV_COLS} FROM events WHERE session_id=$1 ORDER BY id LIMIT 500", sid)
    return {"messages": [_row(m) for m in msgs], "events": [_row(e) for e in evs]}


@app.post("/api/sessions/{sid}/review", dependencies=[Depends(auth)])
async def review(sid: str, reviewed: bool = True):
    async with pool.acquire() as c:
        await c.execute("UPDATE messages SET reviewed=$2 WHERE session_id=$1", sid, reviewed)
    return {"ok": True}


@app.get("/api/export/training.jsonl", dependencies=[Depends(auth)])
async def export():
    """İşaretlenmiş oturumları SFT formatında verir (cevaplar elle düzeltilmeli!)."""
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT session_id, role, content FROM messages "
                             "WHERE reviewed AND role IN ('user','assistant') ORDER BY session_id, id")
    out, cur, msgs = [], None, []
    for r in rows:
        if r["session_id"] != cur and msgs:
            out.append(json.dumps({"messages": msgs}, ensure_ascii=False))
            msgs = []
        cur = r["session_id"]
        msgs.append({"role": r["role"], "content": r["content"]})
    if msgs:
        out.append(json.dumps({"messages": msgs}, ensure_ascii=False))
    return PlainTextResponse("\n".join(out) + "\n", media_type="application/x-ndjson")


@app.websocket("/ws/live")
async def live(ws: WebSocket, token: str = ""):
    if not _ok(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue()
    conn = await asyncpg.connect(DSN)
    await conn.add_listener("new_event", lambda *a: q.put_nowait(int(a[3])))
    try:
        while True:
            eid = await q.get()
            r = await pool.fetchrow(f"SELECT {EV_COLS} FROM events WHERE id=$1", eid)
            if r:
                await ws.send_text(json.dumps(_row(r), default=str))
    except Exception:
        pass
    finally:
        await conn.close()
