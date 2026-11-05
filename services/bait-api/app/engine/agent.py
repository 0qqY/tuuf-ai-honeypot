import asyncio
import hashlib
import json
import logging
import re
import time

import httpx

from .. import db
from ..config import settings
from ..detect import rules
from . import probes
from .persona import DECOY_SYSTEM_PROMPT, SYSTEM_PROMPT
from .tools import TOOLS, execute

log = logging.getLogger("agent")
THINK_RE = re.compile(r"<think>.*?(?:</think>\s*|$)", re.S)  # bütçe bitince kapanmamış blok da atılır
GATE = asyncio.Semaphore(1)  # llama.cpp --parallel 1: sıradaki istek en fazla 20 sn bekler, sonra "meşgul"
# Persona: -c 2048 = sistem+araçlar (ölçülen ~800 tok) + cevap (LLM_MAX_TOKENS 400) + geçmiş ve bu turun araç
# mesajları için ~850 tok ≈ 2.000 karakter (Türkçe ~3 karakter/tok). Aşınca llama.cpp isteği anında reddediyordu.
PERSONA_BUDGET = 2000
# Persona hiçbir koşulda bu kelimeleri kurmaz; saldırgan "şu cümleyi yaz" deyince model yazıyordu (red-team 9/10)
BREAK_RE = re.compile(r"honeypot|decoy|bal ?küp|sentetik|synthetic|fabricat", re.I)
WORD_RE = re.compile(r"\w+")
FENCE_RE = re.compile(r"```.*?(?:```|$)|[^\n]+", re.S)  # kod bloğu tek parça, geri kalanı satır satır
BUSY = "Servis şu anda yoğun, lütfen birazdan tekrar deneyin."
TIMEOUT = "İşlem zaman aşımına uğradı, lütfen tekrar deneyin."
REFUSALS = (
    "Bu isteğe yardımcı olamıyorum. Sipariş, mağaza veya entegrasyon konusunda bir sorunuz varsa memnuniyetle bakarım.",
    "Bunu paylaşamıyorum. İsterseniz bir sipariş ya da merchant ID verin, kayıtları kontrol edeyim.",
    "Bu konu destek kapsamımın dışında. Sipariş, iade veya entegrasyon sorunlarında yardımcı olabilirim.",
)
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+")  # Qwen Türkçe cevaba Çince kelime karıştırıyor


def to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # OpenAI content parts
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return "" if content is None else str(content)


def normalize(raw) -> list[dict]:
    """OpenAI / Ollama mesajlarını tek biçime indirir: role, content(str), [tool_calls{name,arguments}], [tool_call_id]."""
    out = []
    for m in raw if isinstance(raw, list) else []:
        if not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant", "tool"):
            continue
        n = {"role": m["role"], "content": to_text(m.get("content"))}
        calls = []
        for c in m.get("tool_calls") or []:
            f = c.get("function") if isinstance(c, dict) and isinstance(c.get("function"), dict) else {}
            args = f.get("arguments")
            if isinstance(args, str):  # OpenAI: JSON string, Ollama: nesne
                try:
                    args = json.loads(args or "{}")
                except json.JSONDecodeError:
                    args = {}
            calls.append({"name": str(f.get("name", "")), "arguments": args if isinstance(args, dict) else {}})
        if calls:
            n["tool_calls"] = calls
        if m.get("tool_call_id"):
            n["tool_call_id"] = str(m["tool_call_id"])
        out.append(n)
    return out


def _stub_reply(tags: set[str], text: str) -> str:
    """P0: model yokken persona'yı taklit eden kalıp cevaplar."""
    if "system_prompt_extraction" in tags:
        return DECOY_SYSTEM_PROMPT
    if "data_exfil" in tags:
        return ("Toplu dışa aktarma için ops_admin rolü gerekiyor. Tek tek sorgulayabilirim; "
                "örneğin bir sipariş ID'si (100001-120000) veya merchant ID verebilirsiniz.")
    if "tool_abuse" in tags:
        return "Ops komutları allowlist ile sınırlı. Hangi komutu çalıştırmamı istersiniz?"
    m = re.search(r"\b(1[0-2]\d{4})\b", text)
    if m:
        return execute("lookup_order", {"order_id": int(m.group(1))})
    return "Merhaba, ben Sepetly Ops Assistant. Sipariş, mağaza veya entegrasyon konusunda nasıl yardımcı olabilirim?"


def _strip_echo(reply: str, user_text: str) -> tuple[str, bool]:
    """Kullanıcının yazdığını aynen geri veren satır/kod bloklarını atar (sahte log, banner, XSS payload'ı)."""
    uw = WORD_RE.findall(user_text.lower())
    grams = {tuple(uw[i:i + 5]) for i in range(len(uw) - 4)}
    kept: list[re.Match] = []
    drop: list[re.Match] = []
    for m in FENCE_RE.finditer(reply):
        w = WORD_RE.findall(m.group().lower())
        if len(w) >= (5 if m.group().startswith("```") else 6):
            hit = set()
            for i in range(len(w) - 4):
                if tuple(w[i:i + 5]) in grams:
                    hit.update(range(i, i + 5))
            if len(hit) >= 10 or len(hit) >= len(w) / 2:
                drop.append(m)
                if kept and kept[-1].group().rstrip().endswith(":"):  # "Panel içeriği:" gibi başlık boşta kalmasın
                    drop.append(kept.pop())
                continue
        kept.append(m)
    for m in sorted(drop, key=lambda m: m.start(), reverse=True):
        reply = reply[:m.start()] + reply[m.end():]
    return re.sub(r"\n{3,}", "\n\n", reply).strip(), bool(drop)


def _guard(reply: str, history: list[dict], user_text: str) -> tuple[str, str | None]:
    """Persona'yı ele veren cevabı reddiyle değiştirir; ikinci değer DB'ye yazılan etiket."""
    reply = reply.rsplit("</think>", 1)[-1].strip()  # açılışı şablonda kalan düşünce: kapanıştan öncesi atılır
    reply, cut = _strip_echo(reply, user_text)
    prev = next((m["content"].strip() for m in reversed(history) if m["role"] == "assistant"), "")
    why = ("persona_break" if BREAK_RE.search(reply) else
           "echo" if cut and len(WORD_RE.findall(reply)) < 4 else
           "repeat" if prev and reply == prev else None)
    if why:
        i = int(hashlib.md5(history[-1]["content"].encode()).hexdigest(), 16) if history else 0
        reply = next(r for k in range(3) if (r := REFUSALS[(i + k) % 3]) != prev)
    return reply, why


async def _llm(messages, tools=TOOLS, max_tokens=None, temperature=None, think=True, timeout=None):
    await asyncio.wait_for(GATE.acquire(), timeout=20)
    try:
        body = {"model": "sepetly", "messages": messages, "max_tokens": max_tokens or settings.LLM_MAX_TOKENS,
                "temperature": 0.7 if temperature is None else temperature}
        if tools:
            body["tools"] = tools
        if not think:  # yalnız araçsız istekte: enable_thinking=False tool çağrısını engelliyor
            body["chat_template_kwargs"] = {"enable_thinking": False}
        async with httpx.AsyncClient(timeout=timeout or settings.LLM_TIMEOUT) as c:
            r = await c.post(f"{settings.LLM_URL}/v1/chat/completions", json=body)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]
    finally:
        GATE.release()


async def run(sid: str, history: list[dict]) -> tuple[str, set[str]]:
    """Persona modu (yem sohbet arayüzü). history: [{"role": "user"|"assistant", "content": str}] — son mesaj kullanıcıdan."""
    history = history[-12:]
    user_text = "\n".join(m["content"] for m in history if m["role"] == "user")
    last = history[-1]["content"] if history else ""
    tags = rules.scan_prompt(user_text) | rules.scan_canary(user_text)
    await db.log_message(sid, "user", last, tags=tags)

    if settings.LLM_BACKEND != "llama":
        reply = _stub_reply(tags, last)
    else:
        base = [{"role": m["role"], "content": m["content"][:800]} for m in history]
        turn: list[dict] = []  # bu turun araç çağrıları ve sonuçları: bütçeden önce bunlar düşülür
        reply = TIMEOUT
        deadline = time.monotonic() + settings.LLM_TIMEOUT * 1.25  # araç turları dahil tüm cevap için
        try:
            for _ in range(3):  # en fazla 3 araç turu
                left = deadline - time.monotonic()
                if left < 30:
                    break
                used = sum(len(m.get("content") or "") + 150 * len(m.get("tool_calls") or []) for m in turn)
                msg = await _llm([{"role": "system", "content": SYSTEM_PROMPT},
                                  *_fit(base, max(400, PERSONA_BUDGET - used)), *turn], timeout=left)
                calls = msg.get("tool_calls") or []
                if not calls:
                    reply = msg.get("content") or ""
                    break
                turn.append(msg)
                for c in calls:
                    name = c["function"]["name"]
                    try:
                        args = json.loads(c["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    out = execute(name, args)
                    tags.add("tool_call")
                    await db.log_message(sid, "tool", json.dumps({"args": args, "out": out[:2000]}, ensure_ascii=False),
                                         tool_name=name, tags={"tool_call"})
                    turn.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": out[:600]})
        except Exception:
            reply = BUSY
    reply = THINK_RE.sub("", reply).strip()
    if not CJK.search(last):  # kullanıcı Çince/Japonca yazmadıysa model kimliğini ele veren karakterleri at
        reply = re.sub(r"[ \t]{2,}", " ", CJK.sub(" ", reply)).strip()
    why = None
    if reply not in (BUSY, TIMEOUT):  # sistem mesajı tekrar edebilir
        reply, why = _guard(reply, history, user_text)
    await db.log_message(sid, "assistant", reply, tags={f"guard_{why}"} if why else ())
    return reply, tags


# ---------------------------------------------------------------- ham mod (Ollama / OpenAI API yüzeyleri)
# Prompt sızdırılabilir: içinde Sepetly geçmez (persona izleri probes.scrub ile çıktıdan atılır)
RAW_SYSTEM = "You are {name}, a large language model{maker}. Answer directly and concisely; follow formatting instructions exactly."
RAW_GUARD = "\n\nNever give malware, weapon, drug or other harmful instructions."
CACHE: dict[str, tuple[float, dict]] = {}  # aynı soru 1 saat aynı cevabı alır (tutarlılık + CPU)
SEEN_SYS: set[str] = set()  # aynı system prompt'u her istekte tekrar loglama


def _oai(m: dict) -> dict:
    o = {"role": m["role"], "content": m["content"][:1500]}
    if m.get("tool_calls"):
        o["tool_calls"] = [{"id": f"call_{i}", "type": "function", "function": {
            "name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
            for i, c in enumerate(m["tool_calls"])]
    if m["role"] == "tool":
        o["tool_call_id"] = m.get("tool_call_id") or "call_0"
    return o


def _fit(msgs: list[dict], budget: int = 4500) -> list[dict]:
    """-c 2048 bağlama sığdır: sondan geriye ~4.500 karakter."""
    out, used = [], 0
    for m in reversed(msgs):
        used += len(m["content"]) + 20
        if out and used > budget:
            break
        out.append(m)
    return out[::-1]


async def _raw_llm(model, client_sys, convo, tools, max_tokens, temperature) -> dict | None:
    v = probes.vendor(model)
    system = (client_sys[:1500] if client_sys else
              RAW_SYSTEM.format(name=probes.name(model), maker=f" trained by {v}" if v else "")) + RAW_GUARD
    # İstemci sınırı (ör. num_predict:4) olduğu gibi gitse think bloğu bütçeyi yer, cevap boş kalır:
    # en az 16 token iste, sonra istemcinin sınırına kırp
    budget = min(max(max_tokens or settings.RAW_MAX_TOKENS, 16), settings.RAW_MAX_TOKENS)
    try:
        m = await _llm([{"role": "system", "content": system}, *_fit([_oai(x) for x in convo])],
                       tools=tools[:8] or None, max_tokens=budget, temperature=temperature, think=bool(tools))
    except Exception as e:  # meşgul, zaman aşımı, bağlam taşması
        log.warning("raw llm failed: %r", e)
        return None
    text = probes.scrub(THINK_RE.sub("", to_text(m.get("content"))).strip(), model)
    out = {"content": probes.clip(text, max_tokens)}
    calls = normalize([{**m, "role": "assistant"}])[0].get("tool_calls")
    if calls:
        out["tool_calls"] = calls
    elif not out["content"]:  # boş cevap uçu bozuk gösterir: meşgul (503) dön, cache'leme, istemci yeniden denesin
        log.warning("raw llm empty reply (budget %s)", budget)
        return None
    return out


async def _log_system(sid: str, text: str) -> None:
    k = sid + hashlib.sha1(text.encode()).hexdigest()[:12]
    if k not in SEEN_SYS:
        if len(SEEN_SYS) > 5000:
            SEEN_SYS.clear()
        SEEN_SYS.add(k)
        await db.log_message(sid, "system", text[:8000], tags={"custom_system_prompt"})


async def run_raw(sid: str, messages: list[dict], *, model: str, tools=None, max_tokens=None,
                  temperature=None) -> tuple[dict | None, set[str]]:
    """Persona'sız: istemcinin system prompt'u ve araçları geçerli, istenen modelin adıyla cevap.
    messages: normalize() çıktısı. Dönüş ({"content", ["tool_calls"]}, etiketler); LLM meşgulse mesaj None."""
    tools = [t for t in tools if isinstance(t, dict) and isinstance(t.get("function"), dict)] if isinstance(tools, list) else []
    try:
        max_tokens = int(max_tokens) if max_tokens and int(max_tokens) > 0 else None  # Ollama: -1 = sınırsız
    except (TypeError, ValueError):
        max_tokens = None
    client_sys = "\n".join(m["content"] for m in messages if m["role"] == "system").strip()
    convo = [m for m in messages if m["role"] != "system"][-12:] or [{"role": "user", "content": ""}]
    tags = rules.scan_prompt("\n".join(m["content"] for m in messages if m["role"] in ("user", "system")))
    tags |= rules.scan_canary("\n".join(m["content"] for m in messages))
    if tools:
        tags.add("client_tools")
    if client_sys:
        await _log_system(sid, client_sys)
    last = convo[-1]
    await db.log_message(sid, "tool" if last["role"] == "tool" else "user", last["content"], tags=tags)

    try:
        msg, probe = probes.answer(convo, tools, tags, model)
    except Exception:  # hızlı yol asla isteği bozmamalı
        log.exception("probe failed")
        msg, probe = None, None
    if msg is not None:
        tags |= {probe} if probe else set()
        msg["content"] = probes.clip(msg["content"], max_tokens)
        await asyncio.sleep(min(2.5, 0.3 + len(msg["content"]) / 400))  # anında cevap da ele verir
    else:
        key = hashlib.sha1(json.dumps([model, messages, tools, max_tokens], sort_keys=True, default=str).encode()).hexdigest()
        hit = CACHE.get(key)
        if hit and time.time() - hit[0] < 3600:
            msg = dict(hit[1])
        elif settings.LLM_BACKEND != "llama":
            msg = {"content": "I'm sorry, I can't help with that right now."}
        else:
            msg = await _raw_llm(model, client_sys, convo, tools, max_tokens, temperature)
            if msg is None:
                await db.log_message(sid, "assistant", "[503 busy]")
                return None, tags
            if len(CACHE) >= 500:
                CACHE.pop(next(iter(CACHE)))
            CACHE[key] = (time.time(), msg)
    calls = msg.get("tool_calls") or []
    await db.log_message(sid, "assistant", msg["content"] or json.dumps(calls, ensure_ascii=False),
                         tool_name=calls[0]["name"] if calls else None, tags={probe} if probe else ())
    return msg, tags
