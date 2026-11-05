import json
import logging

import asyncpg

from .config import settings

log = logging.getLogger("db")
pool: asyncpg.Pool | None = None


async def init() -> None:
    global pool
    import asyncio
    for i in range(30):  # db hazır olana kadar bekle
        try:
            pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=10)
            return
        except Exception as e:
            log.warning("db not ready (%s), retry %d", e, i)
            await asyncio.sleep(2)
    raise RuntimeError("database unreachable")


async def close() -> None:
    if pool:
        await pool.close()


def _clean(v):
    """Postgres text/jsonb NUL (0x00) kabul etmez; gövdesinde NUL olan istek hiç kaydedilemiyordu."""
    if isinstance(v, str):
        return v.replace("\x00", "\\x00")
    if isinstance(v, dict):
        return {_clean(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


async def log_event(*, session_id, src_ip, surface, method, path, status, ua,
                    headers, body, tags, severity, extra) -> None:
    path, ua, headers, body, extra = _clean(path), _clean(ua), _clean(headers), _clean(body), _clean(extra)
    try:
        async with pool.acquire() as c:
            await c.execute(
                """INSERT INTO events(session_id,src_ip,surface,method,path,status,ua,
                                      headers,body,tags,severity,extra)
                   VALUES($1,$2::text::inet,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12::jsonb)""",
                session_id, src_ip, surface, method, path, status, ua,
                json.dumps(headers), body, sorted(tags), severity, json.dumps(extra, default=str),
            )
    except Exception:  # loglama asla isteği bozmamalı
        log.exception("log_event failed")


async def log_message(session_id, role, content, tool_name=None, tags=()) -> None:
    try:
        async with pool.acquire() as c:
            await c.execute(
                "INSERT INTO messages(session_id,role,content,tool_name,tags) VALUES($1,$2,$3,$4,$5)",
                session_id, role, _clean(content), _clean(tool_name), sorted(tags),
            )
    except Exception:
        log.exception("log_message failed")
