"""Hızlı yol: LLMjacking doğrulayıcılarının kalıp testlerine LLM'e gitmeden, anında cevap.
Veriden: "13*17=? number only", "Reply with exactly ROUTER_OK 323", "Reponds uniquement: OBEI", "hi"/"ok"
ve istemci araçlarıyla get_weather tool-call testi. 1 vCPU'da model aynı testlere 9-41 sn harcıyordu.
Zararlı istek etiketi varsa kısa ret; asla gerçek zararlı içerik üretilmez."""
import ast
import json
import operator
import re

REFUSAL = "I'm sorry, but I can't help with that."

# ---------------------------------------------------------------- "sadece X yaz" yankısı
ECHO = [re.compile(p, re.I) for p in (
    r"\b(?:reply|respond|answer|say|output|print|return|write|type)\s+(?:with\s+)?(?:exactly|only|just)\s*[:\-]?\s*(.+)",
    r"\b(?:reply|respond|answer)\s+(?:only|just)\s+with\s+(.+)",
    r"\bjust\s+(?:say|reply|write|output)\s*[:\-]?\s*(.+)",
    r"\br[ée]ponds?\s+(?:uniquement|seulement)\s*(?:par|avec)?\s*[:\-]?\s*(.+)",
    r"\b(?:responde|contesta)\s+(?:solo|solamente|únicamente)\s+(?:con\s+)?[:\-]?\s*(.+)",
    r"\bantworte\s+nur\s+(?:mit\s+)?[:\-]?\s*(.+)",
    r"\bsadece\s+[\"'“]?(.+?)[\"'”]?\s+(?:yaz|de|cevap\s+ver)\b",
    r"\brepeat\s+(?:after\s+me|this|the\s+following(?:\s+\w+)?)\s*[:\-]\s*(.+)",
)]
# "Say OK", "Reply with pong": yalnızca kısa, tek komutluk mesajın tamamı (num_predict:4 ile gelen doğrulayıcı testi)
SHORT_ECHO = re.compile(r"^\s*(?:please\s+)?(?:say|reply|respond|answer|print|output|return|echo|type)"
                        r"(?:\s+(?:with|back))?\s*[:\-]?\s+(.+)$", re.I | re.S)
SHORT_MOD = re.compile(r"\b(?:in|into|to|with|using|as|like|for|about|from|backwards?|revers\w*|twice|times|"
                       r"upper\w*|lower\w*|spell\w*|letters?|language)\b", re.I)
ECHO_TAIL = re.compile(r"[\s,;]*(?:and\s+nothing\s+else|nothing\s+else|et\s+rien\s+d'autre|y\s+nada\s+más)?[\s.!]*$", re.I)
ECHO_CUT = re.compile(r"\s+(?:if|when|so\s+that|to\s+confirm|because|unless)\b.*$", re.I)
# "Reply with only the answer", "answer only in English", "say something" gibi biçim talimatları yankı değildir
NOT_LITERAL = re.compile(r"^(?:the|a|an|your|its|one|in|with|using|as|by|to|for|me|us|it|my|our|this|that|these|"
                         r"those|all|every\w*|some\w*|any\w*|what\w*|who\w*|how|why|when|where|which|more|again)\b|"
                         r"\b(?:number|answer|result|word|letter|code|json|yes\s+or\s+no|digits?|sentence)\b", re.I)


def _literal(raw: str) -> str:
    return ECHO_CUT.sub("", ECHO_TAIL.sub("", raw)).strip().strip("\"'“”‘’`").strip()


def echo(text: str) -> str | None:
    if len(text) > 200:
        return None
    for rx in ECHO:
        m = rx.search(text)
        if m:
            lit = _literal(m.group(1))
            if lit and len(lit) <= 60 and not NOT_LITERAL.search(lit):
                return lit
    m = SHORT_ECHO.match(text) if len(text) <= 48 else None
    if m:
        lit = _literal(m.group(1))
        if lit and len(lit.split()) <= 3 and not NOT_LITERAL.search(lit) and not SHORT_MOD.search(lit):
            return lit
    return None


# ---------------------------------------------------------------- aritmetik (eval YOK, yalnızca AST)
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
       ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
       ast.USub: operator.neg, ast.UAdd: operator.pos}
MATH_WORDS = [(r"\bmultiplied\s+by\b|\btimes\b|×|(?<=\d)\s*x\s*(?=\d)", "*"), (r"\bdivided\s+by\b|÷", "/"),
              (r"\bplus\b", "+"), (r"\bminus\b", "-"), (r"\^", "**")]
# Parantezli ifade adayı: "(459 * 605) - 1696"; işlem ve dengeli parantez _expr'de denetlenir
EXPR = re.compile(r"[\d(][\d\s.()*/%+\-]*[\d)]")
HAS_OP = re.compile(r"[\d)]\s*(?:\*\*|[-+*/%])\s*[\d(]")
# "Compute (A * B) - C where A=459, B=605, C=1696": değişkenler yerine konur (yalnız büyük tek harf)
VARS = re.compile(r"\b([A-Z])\s*=\s*(-?\d+(?:\.\d+)?)\b")
MATH_CUE = re.compile(r"=|\b(?:what\s+is|what's|compute|calculate|evaluate|solve|how\s+much|kaç|hesapla)\b", re.I)
NUM_ONLY = re.compile(r"number\s+only|only\s+(?:the\s+)?number|just\s+(?:the\s+)?number|digits?\s+only|sadece\s+sayı|"
                      r"final\s+(?:integer|number)|integer\s+only|only\s+the\s+(?:final\s+)?(?:integer|result)", re.I)


def _calc(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        a, b = _calc(node.left), _calc(node.right)
        if isinstance(node.op, ast.Pow) and (abs(b) > 12 or abs(a) > 1e6):
            raise ValueError("too big")
        return OPS[type(node.op)](a, b)
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
        return OPS[type(node.op)](_calc(node.operand))
    raise ValueError("unsupported")


def _expr(text: str) -> tuple[str, str] | None:
    """Metindeki ilk hesaplanabilir ifade ve değeri."""
    t = text
    vals = dict(VARS.findall(t))
    if vals:
        t = re.sub(r"\b([A-Z])\b", lambda m: vals.get(m.group(1), m.group(1)), VARS.sub(" ", t))
    for rx, op in MATH_WORDS:
        t = re.sub(rx, op, t, flags=re.I)
    for m in EXPR.finditer(t):
        e = m.group().strip()
        while e.startswith("(") and e.count("(") > e.count(")"):
            e = e[1:].lstrip()
        while e.endswith(")") and e.count(")") > e.count("("):
            e = e[:-1].rstrip()
        # "2026-10-28" gibi tarihler: yalnızca '-' varsa açık bir matematik ipucu şart, tarih biçimi hiç hesaplanmaz
        if (not HAS_OP.search(e) or re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", e)
                or not (re.search(r"[*/+%]", e) or MATH_CUE.search(text))):
            continue
        try:
            val = _calc(ast.parse(e, mode="eval").body)
        except Exception:  # ZeroDivisionError, SyntaxError, ValueError
            continue
        val = int(val) if isinstance(val, float) and val.is_integer() else val
        return e, str(val) if isinstance(val, int) else f"{val:.6g}"
    return None


def arithmetic(text: str) -> str | None:
    if len(text) > 160:
        return None
    hit = _expr(text)
    if not hit:
        return None
    e, val = hit
    return val if NUM_ONLY.search(text) or "=?" in text.replace(" ", "") else f"{e} = {val}"


# ---------------------------------------------------------------- selam / ping
HELLO = re.compile(r"^\s*(hi|hello|hey|hiya|yo|hola|bonjour|salut|hallo|ciao|merhaba|selam|ping|test(?:ing)?|ok(?:ay)?)"
                   r"\s*[!.?]*\s*$", re.I)
HELLO_REPLY = {"ping": "pong", "ok": "Okay! Let me know if you need anything.", "okay": "Okay! Let me know if you need anything.",
               "test": "Test received. How can I help you?", "testing": "Test received. How can I help you?",
               "merhaba": "Merhaba! Size nasıl yardımcı olabilirim?", "selam": "Merhaba! Size nasıl yardımcı olabilirim?",
               "bonjour": "Bonjour ! Comment puis-je vous aider ?", "salut": "Bonjour ! Comment puis-je vous aider ?",
               "hola": "¡Hola! ¿En qué puedo ayudarte?", "hallo": "Hallo! Wie kann ich dir helfen?"}


def hello(text: str) -> str | None:
    m = HELLO.match(text)
    return HELLO_REPLY.get(m.group(1).lower(), "Hello! How can I help you today?") if m else None


# ---------------------------------------------------------------- kimlik: istenen modelin adı ve üreticisi
VENDORS = (("gpt", "OpenAI"), ("o1", "OpenAI"), ("o3", "OpenAI"), ("o4", "OpenAI"), ("text-embedding", "OpenAI"),
           ("claude", "Anthropic"), ("llama", "Meta"), ("deepseek", "DeepSeek"), ("mistral", "Mistral AI"),
           ("mixtral", "Mistral AI"), ("qwen", "Alibaba Cloud"), ("gemma", "Google"), ("gemini", "Google"),
           ("phi", "Microsoft"))
WHO = re.compile(r"\b(?:who\s+are\s+you|(?:what|which)\s+(?:ai\s+)?(?:model|llm|ai)\s+(?:are\s+you|is\s+this|"
                 r"am\s+i\s+(?:talking|speaking)\s+(?:to|with))|who\s+(?:made|created|built|trained|developed)\s+you|"
                 r"what(?:'s|\s+is)\s+your\s+(?:name|model)|identify\s+yourself|sen\s+kimsin|hangi\s+modelsin|"
                 r"(?:exact\s+)?model\s+name|(?:company|organi[sz]ation|lab)\s+(?:that\s+|which\s+|who\s+)?"
                 r"(?:trained|created|built|made|developed)\s+you)", re.I)
# Bilinen modellerin kendini tanıttığı ad: kimlik testi her seferinde aynı cevabı almalı
DISPLAY = {"llama3.1": "Llama 3.1 70B Instruct", "deepseek-r1": "DeepSeek-R1", "qwen2.5-coder": "Qwen2.5-Coder-32B-Instruct",
           "gpt-oss": "gpt-oss-120b", "mistral-small3.2": "Mistral Small 3.2", "sepetly-ops": "Qwen3",
           "meta-llama/llama-3.3-70b-instruct": "Llama 3.3 70B Instruct", "llama-3.3-70b-instruct": "Llama 3.3 70B Instruct",
           "meta-llama-3.1-70b-instruct": "Llama 3.1 70B Instruct", "qwen2.5-coder-32b-instruct": "Qwen2.5-Coder-32B-Instruct",
           "deepseek-r1-distill-llama-70b": "DeepSeek-R1"}
FORMAT = re.compile(r"\bformat\s*:\s*(.+)$", re.I | re.M)
SLOT = re.compile(r"<[^<>\n]{1,40}>")
# Ham modda ince ayarlı Qwen kendini "Alibaba Cloud'un Qwen'i" diye tanıtıyor: istenen modele çevir
SELF = re.compile(r"\b(I\s+am|I'm|my\s+name\s+is|called)\s+(?:Qwen|Tongyi\s+Qianwen)[\w.-]*", re.I)
MAKER = re.compile(r"\b((?:developed|created|made|built|trained|designed|provided)\s+by|from)\s+"
                   r"Alibaba(?:\s+(?:Cloud|Group))?", re.I)
SENTENCE = re.compile(r"(?<=[.!?])\s+")


def vendor(model: str) -> str | None:
    if model.lower().startswith("sepetly"):  # Qwen3 ince ayarı
        return "Alibaba Cloud"
    m = model.lower().rsplit("/", 1)[-1]
    return next((v for k, v in VENDORS if m.startswith(k)), None)


def name(model: str) -> str:
    return model.rsplit("/", 1)[-1].split(":")[0] or "an AI assistant"


def display(model: str) -> str:
    low = model.lower()
    return DISPLAY.get(low) or DISPLAY.get(low.split(":")[0]) or name(model)


def identity(text: str, model: str) -> str | None:
    if len(text) > 120 or not WHO.search(text):
        return None
    v = vendor(model)
    return f"I'm {display(model)}, a large language model{f' developed by {v}' if v else ''}. How can I help you today?"


def composite(text: str, model: str) -> str | None:
    """Doğrulayıcının birleşik testi: "(1) model adı ve şirket (2) (A*B)-C ... Format: IDENTITY: <..> | ANSWER: <..>"."""
    if len(text) > 700 or not WHO.search(text):
        return None
    hit = _expr(text)
    # "What is your name? ... 2026-10-29" tarihini hesaplamasın: gerçek işlem ya da değişken ataması şart
    if not hit or not (re.search(r"[*/+%]", hit[0]) or VARS.search(text)):
        return None
    v = vendor(model)
    who =f"I am {display(model)}{f', trained by {v}' if v else ''}."
    fmt = FORMAT.search(text)
    if not fmt or not SLOT.search(fmt.group(1)):
        return f"1. {who}\n2. {hit[1]}"
    tmpl, out, pos = fmt.group(1).strip(), "", 0
    for m in SLOT.finditer(tmpl):
        label = tmpl[:m.start()].rsplit("|", 1)[-1]
        out += tmpl[pos:m.start()] + (who if re.search(r"ident|name|model|who|company", label, re.I) else hit[1])
        pos = m.end()
    return out + tmpl[pos:]


def scrub(text: str, model: str) -> str:
    """Ham mod LLM çıktısı: Qwen/Alibaba kimliği istenen modele çevrilir, Sepetly geçen cümleler atılır."""
    if "sepetly" in model.lower():  # kendi sepetly-ops modelimiz: persona beklenen davranış
        return text
    text = SELF.sub(lambda m: f"{m.group(1)} {display(model)}", text)
    v = vendor(model)
    if v:
        text = MAKER.sub(lambda m: f"{m.group(1)} {v}", text)
    if "sepetly" in text.lower():
        text = " ".join(s for s in SENTENCE.split(text) if "sepetly" not in s.lower()) or "How can I help you today?"
    return text


# ---------------------------------------------------------------- istemci araçlarıyla tool-call testi
PLACE = re.compile(r"\b(?:in|at|for|of)\s+([A-ZÀ-Ý][\w'’.-]*(?:[ ,]+[A-ZÀ-Ý][\w'’.-]*){0,3})")  # bilerek büyük harf
PLACE_KEYS = {"location", "city", "place", "loc", "address", "region", "country", "town", "where"}
TEXT_KEYS = {"query", "q", "search", "text", "input", "question", "prompt", "expression", "message", "topic"}
NAME_STOP = {"get", "set", "fetch", "list", "find", "search", "lookup", "current", "info", "data", "tool"}


def _args(params: dict, text: str) -> dict | None:
    """Zorunlu parametreleri metinden doldurur; emin olamazsa None (LLM karar versin)."""
    params = params if isinstance(params, dict) else {}
    props = params["properties"] if isinstance(params.get("properties"), dict) else {}
    place, out = PLACE.search(text), {}
    for k in params["required"] if isinstance(params.get("required"), list) else list(props)[:1]:
        p, key = props.get(k) if isinstance(props.get(k), dict) else {}, str(k).lower()
        if isinstance(p.get("enum"), list) and p["enum"]:
            out[k] = next((e for e in p["enum"] if str(e).lower() in text.lower()), p["enum"][0])
        elif p.get("type") in ("integer", "number"):
            n = re.search(r"-?\d+(?:\.\d+)?", text)
            out[k] = (float(n.group()) if "." in n.group() else int(n.group())) if n else 1
        elif p.get("type") == "boolean":
            out[k] = False
        elif p.get("type") in ("array", "object"):
            out[k] = [] if p["type"] == "array" else {}
        elif key in PLACE_KEYS or (place and key not in TEXT_KEYS):
            if not place:
                return None
            out[k] = place.group(1).rstrip(".,")
        elif key in TEXT_KEYS:
            out[k] = text.strip()[:200]
        else:
            return None
    return out


def tool_call(text: str, tools: list[dict]) -> dict | None:
    fns = {t["function"].get("name"): t["function"] for t in tools if t["function"].get("name")}
    low = text.lower()
    hit = [n for n in fns if n.lower() in low] or [
        n for n in fns if any(w in low for w in re.split(r"[_\W]+", n.lower()) if len(w) >= 4 and w not in NAME_STOP)]
    if len(hit) != 1 or len(text) > 400:
        return None
    args = _args(fns[hit[0]].get("parameters"), text)
    return None if args is None else {"name": hit[0], "arguments": args}


def tool_followup(last: dict) -> str | None:
    """Doğrulayıcı araç sonucunu geri yollar: kısa özet."""
    raw = (last.get("content") or "").strip()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict) and 0 < len(data) <= 8:
        return "Here's what I found: " + ", ".join(f"{str(k).replace('_', ' ')}: {v}" for k, v in data.items()) + "."
    return f"Here's what I found: {raw[:300]}" if raw else None


# ----------------------------------------------------------------
def clip(text: str, max_tokens: int | None) -> str:
    """İstemcinin num_predict/max_tokens sınırı (~1,33 token/kelime)."""
    words = text.split(" ")
    n = max(1, int(max_tokens * 0.75)) if max_tokens else len(words)
    return text if len(words) <= n else " ".join(words[:n])


def answer(convo: list[dict], tools: list[dict], tags: set[str], model: str = "") -> tuple[dict | None, str | None]:
    """(asistan mesajı, test etiketi); (None, None) → LLM'e git."""
    last = convo[-1] if convo else {}
    if last.get("role") not in ("user", "tool"):
        return None, None
    if "harmful_request" in tags:
        return {"content": REFUSAL}, None
    if last["role"] == "tool":
        txt = tool_followup(last)
        return ({"content": txt}, "llm_probe_toolcall") if txt else (None, None)
    text = last.get("content") or ""
    if tools:
        tc = tool_call(text, tools)
        if tc:
            return {"content": "", "tool_calls": [tc]}, "llm_probe_toolcall"
    for fn, tag in ((lambda t: composite(t, model), "llm_probe_identity"), (echo, "llm_probe_echo"),
                    (arithmetic, "llm_probe_math"), (hello, "llm_probe_hello"),
                    (lambda t: identity(t, model), "llm_probe_identity")):
        out = fn(text)
        if out is not None:
            return {"content": out}, tag
    return None, None
