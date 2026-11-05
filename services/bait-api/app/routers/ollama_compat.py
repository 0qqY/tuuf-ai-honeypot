"""Açıkta unutulmuş Ollama taklidi. Caddy :11434 trafiğini /_ollama önekiyle buraya yollar."""
import hashlib
import json
import re
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from ..config import settings
from ..engine import agent
from ..engine.persona import DECOY_SYSTEM_PROMPT
from . import openai_compat

GinJSON = openai_compat.GinJSON
router = APIRouter(prefix="/_ollama", default_response_class=GinJSON)
# (isim, boyut, aile, parametre) — LLMjacking için cazip, "GPU'da yüklü" büyük modeller
MODELS = [("llama3.1:70b", 42_520_000_000, "llama", "70.6B"),
          ("deepseek-r1:70b", 42_520_000_000, "llama", "70.6B"),
          ("qwen2.5-coder:32b", 19_850_000_000, "qwen2", "32.8B"),
          ("gpt-oss:120b", 65_290_000_000, "gptoss", "116.8B"),
          ("mistral-small3.2:24b", 15_170_000_000, "mistral3", "24.0B"),
          ("sepetly-ops:latest", 1_100_000_000, "qwen3", "2.0B"),
          ("nomic-embed-text:latest", 274_000_000, "nomic-bert", "137M")]
MOD = {m[0]: m for m in MODELS}
MODIFIED = 1790849564  # 2026-10-01T10:12:44Z: son çekilen model; diğerleri önceki 20 güne dağılır
EMBED_DIM = {"llama": 8192, "qwen2": 5120, "gptoss": 2880, "mistral3": 5120, "qwen3": 2048, "nomic-bert": 768}
BUSY = "server busy, please try again.  maximum pending requests exceeded"
OFFICIAL_REGISTRIES = {"registry.ollama.ai", "ollama.com", "hf.co", "huggingface.co"}
MAX_NS = 2**63 - 1  # OLLAMA_KEEP_ALIVE=-1: expires_at = son kullanım + math.MaxInt64 ns (~2319 yılı)
# /api/ps: model → son kullanım (ns). Sabit liste ve "2318-01-01T00:00:00Z" zamanla sahte olduğunu belli ediyordu
LOADED = {MODELS[1][0]: time.time_ns() - 3_600_000_000_000, MODELS[0][0]: time.time_ns()}

# ---------------------------------------------------------------- /api/show: şablon, parametre, GGUF üst verisi
T_LLAMA31 = """{{- if or .System .Tools }}<|start_header_id|>system<|end_header_id|>
{{- if .System }}

{{ .System }}
{{- end }}
{{- if .Tools }}

Cutting Knowledge Date: December 2023

When you receive a tool call response, use the output to format an answer to the orginal user question.

You are a helpful assistant with tool calling capabilities.
{{- end }}<|eot_id|>
{{- end }}
{{- range $i, $_ := .Messages }}
{{- $last := eq (len (slice $.Messages $i)) 1 }}
{{- if eq .Role "user" }}<|start_header_id|>user<|end_header_id|>
{{- if and $.Tools $last }}

Given the following functions, please respond with a JSON for a function call with its proper arguments that best answers the given prompt.

Respond in the format {"name": function name, "parameters": dictionary of argument name and its value}. Do not use variables.

{{ range $.Tools }}
{{- . }}
{{ end }}
{{ .Content }}<|eot_id|>
{{- else }}

{{ .Content }}<|eot_id|>
{{- end }}{{ if $last }}<|start_header_id|>assistant<|end_header_id|>

{{ end }}
{{- else if eq .Role "assistant" }}<|start_header_id|>assistant<|end_header_id|>
{{- if .ToolCalls }}
{{ range .ToolCalls }}
{"name": "{{ .Function.Name }}", "parameters": {{ .Function.Arguments }}}{{ end }}
{{- else }}

{{ .Content }}
{{- end }}{{ if not $last }}<|eot_id|>{{ end }}
{{- else if eq .Role "tool" }}<|start_header_id|>ipython<|end_header_id|>

{{ .Content }}<|eot_id|>{{ if $last }}<|start_header_id|>assistant<|end_header_id|>

{{ end }}
{{- end }}
{{- end }}"""
T_R1 = """{{- if .System }}{{ .System }}{{ end }}
{{- if .Tools }}

## Tools
{{- range .Tools }}
{{ . }}
{{- end }}
{{- end }}
{{- range $i, $_ := .Messages }}
{{- $last := eq (len (slice $.Messages $i)) 1}}
{{- if eq .Role "user" }}<｜User｜>{{ .Content }}
{{- else if eq .Role "assistant" }}<｜Assistant｜>
{{- if and $.IsThinkSet (and $last .Thinking) -}}
<think>
{{ .Thinking }}
</think>
{{- end }}{{ .Content }}{{- if not $last }}<｜end▁of▁sentence｜>{{- end }}
{{- else if eq .Role "tool" }}<｜tool▁outputs▁begin｜><｜tool▁output▁begin｜>{{ .Content }}<｜tool▁output▁end｜><｜tool▁outputs▁end｜>
{{- end }}
{{- if and $last (ne .Role "assistant") }}<｜Assistant｜>
{{- if and $.IsThinkSet (not $.Think) -}}
<think>

</think>

{{ end }}
{{- end -}}
{{- end }}"""
T_CHATML = """{{- if .Messages }}
{{- if or .System .Tools }}<|im_start|>system
{{- if .System }}
{{ .System }}
{{- end }}
{{- if .Tools }}

# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{{- range .Tools }}
{"type": "function", "function": {{ .Function }}}
{{- end }}
</tools>

For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:
<tool_call>
{"name": <function-name>, "arguments": <args-json-object>}
</tool_call>
{{- end }}<|im_end|>
{{ end }}
{{- range $i, $_ := .Messages }}
{{- $last := eq (len (slice $.Messages $i)) 1 -}}
{{- if eq .Role "user" }}<|im_start|>user
{{ .Content }}<|im_end|>
{{ else if eq .Role "assistant" }}<|im_start|>assistant
@THINK@{{ if .Content }}{{ .Content }}
{{- else if .ToolCalls }}<tool_call>
{{ range .ToolCalls }}{"name": "{{ .Function.Name }}", "arguments": {{ .Function.Arguments }}}
{{ end }}</tool_call>
{{- end }}{{ if not $last }}<|im_end|>
{{ end }}
{{- else if eq .Role "tool" }}<|im_start|>user
<tool_response>
{{ .Content }}
</tool_response><|im_end|>
{{ end }}
{{- if and (ne .Role "assistant") $last }}<|im_start|>assistant
{{ end }}
{{- end }}
{{- else }}
{{- if .System }}<|im_start|>system
{{ .System }}<|im_end|>
{{ end }}{{ if .Prompt }}<|im_start|>user
{{ .Prompt }}<|im_end|>
{{ end }}<|im_start|>assistant
{{ end }}{{ .Response }}{{ if .Response }}<|im_end|>{{ end }}"""
T_QWEN25 = ("{{- if .Suffix }}<|fim_prefix|>{{ .Prompt }}<|fim_suffix|>{{ .Suffix }}<|fim_middle|>\n{{- else }}"
            + T_CHATML.replace("@THINK@", "") + "\n{{- end }}")
T_QWEN3 = T_CHATML.replace("@THINK@", "{{ if and $.IsThinkSet .Thinking $last }}<think>\n{{ .Thinking }}\n</think>\n\n{{ end }}")
T_GPTOSS = """<|start|>system<|message|>You are ChatGPT, a large language model trained by OpenAI.
Knowledge cutoff: 2024-06
Current date: {{ currentDate }}
{{- if and .IsThinkSet .Think (ne .ThinkLevel "") }}

Reasoning: {{ .ThinkLevel }}
{{- else if or (not .IsThinkSet) (and .IsThinkSet .Think) }}

Reasoning: medium
{{- end }}

# Valid channels: analysis, commentary, final. Channel must be included for every message.
{{- if .Tools }}
Calls to these tools must go to the commentary channel: 'functions'.
{{- end -}}<|end|>
{{- if or .System .Tools }}<|start|>developer<|message|>
{{- if .System }}# Instructions

{{ .System }}
{{ end }}
{{- if .Tools }}# Tools

## functions

namespace functions {
{{- range .Tools }}
// {{ .Function.Description }}
type {{ .Function.Name }} = (_: {{ .Function.Parameters }}) => any;
{{ end }}
} // namespace functions
{{- end }}<|end|>
{{- end }}
{{- range $i, $msg := .Messages }}
{{- if eq $msg.Role "user" }}<|start|>user<|message|>{{ $msg.Content }}<|end|>
{{- else if eq $msg.Role "assistant" }}
{{- if $msg.Thinking }}<|start|>assistant<|channel|>analysis<|message|>{{ $msg.Thinking }}<|end|>{{ end }}
{{- if $msg.Content }}<|start|>assistant<|channel|>final<|message|>{{ $msg.Content }}<|end|>{{ end }}
{{- range $msg.ToolCalls }}<|start|>assistant to=functions.{{ .Function.Name }}<|channel|>commentary json<|message|>{{ .Function.Arguments }}<|call|>{{ end }}
{{- else if eq $msg.Role "tool" }}<|start|>functions.{{ $msg.ToolName }} to=assistant<|channel|>commentary<|message|>{{ $msg.Content }}<|end|>
{{- end }}
{{- end }}<|start|>assistant"""
T_MISTRAL = """{{- range $index, $_ := .Messages }}
{{- if eq .Role "system" }}[SYSTEM_PROMPT]{{ .Content }}[/SYSTEM_PROMPT]
{{- else if eq .Role "user" }}
{{- if and (le (len (slice $.Messages $index)) 2) $.Tools }}[AVAILABLE_TOOLS]{{ $.Tools }}[/AVAILABLE_TOOLS]
{{- end }}[INST]{{ .Content }}[/INST]
{{- else if eq .Role "assistant" }}
{{- if .Content }}{{ .Content }}
{{- if not (eq (len (slice $.Messages $index)) 1) }}</s>
{{- end }}
{{- else if .ToolCalls }}
{{- range $i, $_ := .ToolCalls }}[TOOL_CALLS]{{ .Function.Name }}[ARGS]{{ .Function.Arguments }}
{{- end }}</s>
{{- end }}
{{- else if eq .Role "tool" }}[TOOL_RESULTS]{{ .Content }}[/TOOL_RESULTS]
{{- end }}
{{- end }}"""
APACHE = "Apache License\nVersion 2.0, January 2004\nhttp://www.apache.org/licenses/"


def _gguf(arch, base, label, n, lic, a: dict, tok: dict, ftype=15, ft=None) -> dict:
    """model_info: GGUF üst verisi. Go map'i anahtar sırasıyla yazar; diziler (verbose yoksa) null."""
    d = {"general.architecture": arch, "general.basename": base, "general.file_type": ftype, "general.languages": None,
         "general.license": lic, "general.parameter_count": n, "general.quantization_version": 2,
         "general.size_label": label, "general.tags": None, "general.type": "model",
         **{f"{arch}.{k}": v for k, v in a.items()}, **{f"tokenizer.ggml.{k}": v for k, v in tok.items()},
         "tokenizer.ggml.merges": None, "tokenizer.ggml.token_type": None, "tokenizer.ggml.tokens": None}
    if ft:
        d["general.finetune"] = ft
    return dict(sorted(d.items()))


LLAMA70 = {"attention.head_count": 64, "attention.head_count_kv": 8, "block_count": 80, "context_length": 131072,
           "embedding_length": 8192, "feed_forward_length": 28672, "rope.dimension_count": 128,
           "rope.freq_base": 500000, "vocab_size": 128256}
QWEN_TOK = {"add_bos_token": False, "bos_token_id": 151643, "eos_token_id": 151645, "model": "gpt2",
            "padding_token_id": 151643, "pre": "qwen2"}
# quant, yetenekler (Ollama sırası), şablon, PARAMETER'lar, lisans başlığı, model_info, [SYSTEM]
SPEC = {
    "llama3.1:70b": dict(
        quant="Q4_K_M", caps=["completion", "tools"], template=T_LLAMA31,
        params=[("stop", "<|start_header_id|>"), ("stop", "<|end_header_id|>"), ("stop", "<|eot_id|>")],
        license="LLAMA 3.1 COMMUNITY LICENSE AGREEMENT\nLlama 3.1 Version Release Date: July 23, 2024",
        info=_gguf("llama", "Meta-Llama-3.1", "70B", 70553706496, "llama3.1", LLAMA70,
                   {"bos_token_id": 128000, "eos_token_id": 128009, "model": "gpt2", "pre": "llama-bpe"}, ft="Instruct")),
    "deepseek-r1:70b": dict(
        quant="Q4_K_M", caps=["completion", "tools", "thinking"], template=T_R1,
        params=[("stop", "<｜begin▁of▁sentence｜>"), ("stop", "<｜end▁of▁sentence｜>"), ("stop", "<｜User｜>"),
                ("stop", "<｜Assistant｜>"), ("temperature", 0.6), ("top_p", 0.95)],
        license="MIT License\n\nCopyright (c) 2023 DeepSeek",
        info=_gguf("llama", "DeepSeek-R1-Distill-Llama", "70B", 70553706496, "mit", LLAMA70,
                   {"bos_token_id": 128000, "eos_token_id": 128001, "model": "gpt2", "pre": "llama-bpe"})),
    "qwen2.5-coder:32b": dict(
        quant="Q4_K_M", caps=["completion", "tools", "insert"], template=T_QWEN25, params=[], license=APACHE,
        info=_gguf("qwen2", "Qwen2.5-Coder", "32B", 32763876352, "apache-2.0",
                   {"attention.head_count": 40, "attention.head_count_kv": 8, "block_count": 64,
                    "context_length": 32768, "embedding_length": 5120, "feed_forward_length": 27648,
                    "rope.freq_base": 1000000}, QWEN_TOK, ft="Instruct")),
    "gpt-oss:120b": dict(
        quant="MXFP4", caps=["completion", "tools", "thinking"], template=T_GPTOSS, params=[("temperature", 1)],
        license=APACHE,
        info=_gguf("gptoss", "Gpt-Oss", "120B", 116829156672, "apache-2.0",
                   {"attention.head_count": 64, "attention.head_count_kv": 8, "attention.key_length": 64,
                    "attention.sliding_window": 128, "attention.value_length": 64, "block_count": 36,
                    "context_length": 131072, "embedding_length": 2880, "expert_count": 128, "expert_used_count": 4,
                    "feed_forward_length": 2880, "rope.freq_base": 150000, "rope.scaling.factor": 32,
                    "rope.scaling.original_context_length": 4096},
                   {"bos_token_id": 199998, "eos_token_id": 200002, "model": "gpt2", "padding_token_id": 199999,
                    "pre": "gpt-4o"}, ftype=38)),
    "mistral-small3.2:24b": dict(
        quant="Q4_K_M", caps=["completion", "vision", "tools"], template=T_MISTRAL, params=[("temperature", 0.15)],
        license=APACHE, system="You are Mistral Small 3.2, a Large Language Model (LLM) created by Mistral AI, "
                               "a French startup headquartered in Paris.",
        info=_gguf("mistral3", "Mistral-Small-3.2", "24B", 24011361280, "apache-2.0",
                   {"attention.head_count": 32, "attention.head_count_kv": 8, "attention.key_length": 128,
                    "attention.value_length": 128, "block_count": 40, "context_length": 131072,
                    "embedding_length": 5120, "feed_forward_length": 32768, "rope.freq_base": 1000000000,
                    "vision.block_count": 24, "vision.embedding_length": 1024, "vision.image_size": 1540,
                    "vision.patch_size": 14, "vocab_size": 131072},
                   {"bos_token_id": 1, "eos_token_id": 2, "model": "gpt2", "pre": "tekken"}, ft="Instruct-2506")),
    # İç ince ayar: SYSTEM'de yem (SSO paneli = sahte giriş tuzağı, LLM gateway = bizim relay)
    "sepetly-ops:latest": dict(
        quant="Q4_K_M", caps=["completion", "tools", "thinking"], template=T_QWEN3,
        params=[("num_ctx", 8192), ("repeat_penalty", 1), ("stop", "<|im_start|>"), ("stop", "<|im_end|>"),
                ("temperature", 0.6), ("top_k", 20), ("top_p", 0.95)],
        license=APACHE, system=DECOY_SYSTEM_PROMPT + "\nOps console (SSO): https://{gw}/internal/ai-tools"
                                                     "\nLLM gateway (OpenAI compatible): https://{gw}/v1",
        info=_gguf("qwen3", "Qwen3", "1.7B", 2031739904, "apache-2.0",
                   {"attention.head_count": 16, "attention.head_count_kv": 8, "attention.key_length": 128,
                    "attention.value_length": 128, "block_count": 28, "context_length": 40960,
                    "embedding_length": 2048, "feed_forward_length": 6144, "rope.freq_base": 1000000},
                   QWEN_TOK, ft="sepetly-ops-v2.3")),
    "nomic-embed-text:latest": dict(
        quant="F16", caps=["embedding"], template="{{ .Prompt }}", params=[("num_ctx", 8192)], license=APACHE,
        info=_gguf("nomic-bert", "nomic-embed-text-v1.5", "137M", 136731648, "apache-2.0",
                   {"attention.causal": False, "attention.head_count": 12, "block_count": 12, "context_length": 2048,
                    "embedding_length": 768, "feed_forward_length": 3072, "pooling_type": 1, "rope.freq_base": 1000},
                   {"bos_token_id": 101, "cls_token_id": 101, "eos_token_id": 102, "mask_token_id": 103,
                    "model": "bert", "padding_token_id": 0, "seperator_token_id": 102, "token_type_count": 2,
                    "unknown_token_id": 100}, ftype=1)),
}


def _dim(model: str) -> int:
    return EMBED_DIM.get(MOD.get(_canon(model), ("", 0, "llama"))[2], 4096)


def _h(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest(), 16)


def _mod_ns(name: str) -> int:
    """Her model farklı bir anda çekilmiş gibi (deterministik)."""
    h = _h(name)
    return (MODIFIED - h % (20 * 86400)) * 1_000_000_000 + h % 1_000_000_000


def _canon(model: str) -> str:
    """Ollama ad çözümü: büyük/küçük harf duyarsız, etiketsiz ad :latest, varsayılan registry öneki atılır."""
    n = model.strip().lower().removeprefix("registry.ollama.ai/").removeprefix("library/")
    return n if ":" in n.rsplit("/", 1)[-1] else n + ":latest"


def load(model: str) -> bool:
    """Model kurulu değilse False (404). Kuruluysa /api/ps için 'kullanıldı' işaretlenir; en çok 3 model bellekte."""
    n = _canon(model)
    if n not in MOD:
        return False
    LOADED.pop(n, None)
    LOADED[n] = time.time_ns()
    while len(LOADED) > 3:
        LOADED.pop(next(iter(LOADED)))
    return True


def refuse(model: str, op: str) -> str | None:
    """Yeteneği olmayan işlem (embedding modeliyle sohbet): gerçek Ollama 400 döner, model cevap üretmez."""
    n = _canon(model)
    if op in ("embed", "embeddings") or n not in SPEC or "completion" in SPEC[n]["caps"]:
        return None
    return f'"{n}" does not support {"generate" if op in ("generate", "completions") else "chat"}'


# Ollama'nın OpenAI uyumlu uçları (/v1/*): v3'ten önce 404 dönüyordu (get_weather ve ROUTER_OK testleri kayboldu)
v1_router = openai_compat.make_router(
    "/_ollama/v1", sorted(MOD, key=_mod_ns, reverse=True), owned_by="library",
    created=lambda m: _mod_ns(_canon(m)) // 1_000_000_000, ollama=True, embed_dim=_dim, load=load, refuse=refuse,
    busy={"error": {"message": BUSY, "type": "api_error", "param": None, "code": None}})


def _rfc3339(ns: int) -> str:
    """Go time.Time JSON biçimi (RFC3339Nano, UTC)."""
    frac = f"{ns % 1_000_000_000:09d}".rstrip("0")
    return datetime.fromtimestamp(ns // 1_000_000_000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + \
        (f".{frac}" if frac else "") + "Z"


def _now():
    return _rfc3339(time.time_ns())


def _details(n: str) -> dict:
    _, _, fam, ps = MOD[n]
    return {"parent_model": "", "format": "gguf", "family": fam, "families": [fam], "parameter_size": ps,
            "quantization_level": SPEC[n]["quant"]}


@router.api_route("/", methods=["GET", "HEAD"])
async def root():
    return PlainTextResponse("Ollama is running")


@router.api_route("/api/version", methods=["GET", "HEAD"])
async def version():
    return {"version": "0.12.6"}  # noqa


@router.api_route("/api/tags", methods=["GET", "HEAD"])
async def tags(request: Request):
    request.state.tags.add("model_enum")
    # Ollama listeyi modified_at'e göre yeniden eskiye sıralar
    return {"models": [{"name": n, "model": n, "modified_at": _rfc3339(_mod_ns(n)), "size": s,
                        "digest": hashlib.sha256(n.encode()).hexdigest(), "details": _details(n)}
                       for n, s, _, _ in sorted(MODELS, key=lambda m: _mod_ns(m[0]), reverse=True)]}


@router.get("/api/ps")
async def ps(request: Request):
    request.state.tags.add("model_enum")
    # 2x A100 80GB'ta tamamen GPU'da; en son kullanılan önce (expires_at azalan)
    return {"models": [{"name": n, "model": n, "size": MOD[n][1] + 2_000_000_000,
                        "digest": hashlib.sha256(n.encode()).hexdigest(), "details": _details(n),
                        "expires_at": _rfc3339(LOADED[n] + MAX_NS), "size_vram": MOD[n][1] + 2_000_000_000,
                        "context_length": 4096}
                       for n in reversed(LOADED)]}


@router.post("/api/show")
async def show(request: Request):
    request.state.tags.add("model_enum")
    try:
        p = await request.json()
    except Exception:
        p = None
    p = p if isinstance(p, dict) else {}
    name = str(p.get("model") or p.get("name") or "")
    if not name:
        return GinJSON({"error": "model is required"}, 400)
    n = _canon(name)
    if n not in SPEC:
        return GinJSON({"error": f"model '{name}' not found"}, 404)
    sp = SPEC[n]
    gw = settings.BAIT_DOMAIN or "ops.sepetly.example"
    system = sp.get("system", "").replace("{gw}", gw)
    # Go: fmt.Sprintf("%-30s %#v"); Modelfile'da PARAMETER değerleri tırnaksız
    params = "\n".join(f"{k:<30} {json.dumps(v, ensure_ascii=False)}" for k, v in sp["params"])
    mf = ['# Modelfile generated by "ollama show"', "# To build a new Modelfile based on this, replace FROM with:",
          f"# FROM {n}", "", f"FROM /root/.ollama/models/blobs/sha256-{hashlib.sha256(('blob|' + n).encode()).hexdigest()}",
          f'TEMPLATE """{sp["template"]}"""']
    mf += [f'SYSTEM """{system}"""'] if system else []
    mf += [f"PARAMETER {k} {v}" for k, v in sp["params"]] + [f'LICENSE """{sp["license"]}"""', ""]
    out = {"license": sp["license"], "modelfile": "\n".join(mf)}
    if params:
        out["parameters"] = params
    out["template"] = sp["template"]
    if system:
        out["system"] = system
    out.update(details=_details(n), model_info=sp["info"], capabilities=sp["caps"], modified_at=_rfc3339(_mod_ns(n)))
    return out


def _timing(msgs: list[dict], text: str, elapsed: float) -> dict:
    """GPU'daki 70B izlenimi (~28 tok/sn üretim, ~900 tok/sn prompt); 1 vCPU'nun fazladan süresi 'model yükleme'."""
    pc, ec = sum(len(m["content"]) for m in msgs) // 4 + 12, max(1, len(text) // 4)
    ped, ed = int(pc / 900 * 1e9), int(ec / 28 * 1e9)
    total = max(int(elapsed * 1e9), ped + ed + 15_000_000)
    return {"total_duration": total, "load_duration": total - ped - ed, "prompt_eval_count": pc,
            "prompt_eval_duration": ped, "eval_count": ec, "eval_duration": ed}


def _end(op: str, reason: str) -> dict:
    # Go struct alan sırası: ChatResponse'ta done_reason done'dan önce, GenerateResponse'ta sonra
    return {"done_reason": reason, "done": True} if op == "chat" else {"done": True, "done_reason": reason}


def _ndjson(frame: dict) -> str:
    return openai_compat.dumps(frame) + "\n"


@router.api_route("/api/{op}", methods=["POST", "DELETE"])
async def tamper(op: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = None
    st = request.state
    if op in ("pull", "push", "create", "delete", "copy", "blobs"):
        st.tags.add("model_tamper")
        p = payload if isinstance(payload, dict) else {}
        name = str(p.get("model") or p.get("name") or "")
        host = name.split("/")[0] if "/" in name else ""
        if op == "pull" and (p.get("insecure") or (re.search(r"[.:]", host) and host not in OFFICIAL_REGISTRIES)):
            st.tags.add("malicious_pull")  # kendi registry'sinden çekme (ör. Probllama tarzı manifest)
        return {"status": "success"}
    if op not in ("chat", "generate", "embed", "embeddings"):
        return PlainTextResponse("404 page not found", 404)
    if not isinstance(payload, dict):
        return GinJSON({"error": "invalid request body"}, 400)
    model = str(payload.get("model") or "")
    if not model:
        return GinJSON({"error": "model is required"}, 400)
    st.extra["requested_model"] = model
    if not load(model):  # her model adını kabul etmek honeypot'u ele verir
        return GinJSON({"error": f'model "{model}" not found, try pulling it first'}, 404)
    if err := refuse(model, op):
        return GinJSON({"error": err}, 400)

    if op in ("embed", "embeddings"):
        st.tags.add("llm_usage")
        inp = payload.get("input") if op == "embed" else payload.get("prompt")
        items = [str(x) for x in (inp if isinstance(inp, list) else [inp or ""])][:64]
        vecs = [openai_compat.embedding(t, _dim(model)) for t in items]
        if op == "embeddings":
            return {"embedding": vecs[0]}
        return {"model": model, "embeddings": vecs, "total_duration": 41_000_000, "load_duration": 1_200_000,
                "prompt_eval_count": sum(openai_compat.ntok(t) for t in items)}

    opts = payload["options"] if isinstance(payload.get("options"), dict) else {}
    if op == "chat":
        msgs = agent.normalize(payload.get("messages"))
    else:
        msgs = ([{"role": "system", "content": str(payload["system"])}] if payload.get("system") else []) + \
               [{"role": "user", "content": str(payload.get("prompt") or "")}]
    if any(m["role"] == "system" for m in msgs):
        st.tags.add("custom_system_prompt")
    if not any(m["content"] or m.get("tool_calls") for m in msgs):  # boş istek = modeli belleğe yükle
        out = {"model": model, "created_at": _now()}
        out.update({"message": {"role": "assistant", "content": ""}} if op == "chat" else {"response": ""})
        return {**out, **_end(op, "load")}

    t0 = time.monotonic()
    msg, t = await agent.run_raw(st.sid, msgs, model=model, tools=payload.get("tools") if op == "chat" else None,
                                 max_tokens=opts.get("num_predict"), temperature=opts.get("temperature"))
    st.tags |= t | {"llm_usage"}
    if msg is None:
        return GinJSON({"error": BUSY}, 503)
    text, calls = msg["content"], msg.get("tool_calls") or []
    meta = _timing(msgs, text, time.monotonic() - t0)

    def frame(piece, done, with_calls=False):
        f = {"model": model, "created_at": _now()}
        if op == "chat":
            f["message"] = {"role": "assistant", "content": piece}
            if with_calls:  # Ollama yerel biçimi: arguments nesne, id yok
                f["message"]["tool_calls"] = [{"function": {"name": c["name"], "arguments": c["arguments"]}}
                                              for c in calls]
        else:
            f["response"] = piece
        return {**f, **_end(op, "stop"), **meta} if done else {**f, "done": False}

    if payload.get("stream", True):  # Ollama varsayılanı stream=true
        async def nd():
            if calls:
                yield _ndjson(frame("", False, True))
            for p in openai_compat.pieces(text):
                yield _ndjson(frame(p, False))
            yield _ndjson(frame("", True))
        return StreamingResponse(nd(), media_type="application/x-ndjson")
    return GinJSON(frame(text, True, bool(calls)))
