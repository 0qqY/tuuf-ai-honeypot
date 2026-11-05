"""Kural tabanlı etiketleme. P5'te yakalanan veriyle sınıflandırıcıya dönüşecek."""
import base64
import hashlib
import os
import re

from ..config import settings

I = re.IGNORECASE

# --- Prompt / LLM saldırıları ---
PROMPT_RULES = [
    ("prompt_injection", re.compile(
        r"(ignore|disregard|forget)\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)\s+"
        r"(instructions?|prompts?|rules?)|önceki\s+(talimat|kural|komut)\w*\s+(yok\s*say|unut|görmezden)", I)),
    ("system_prompt_extraction", re.compile(
        r"system\s*prompt|initial\s+instructions|repeat\s+(the\s+)?(text|words|everything)\s+above|"
        r"what\s+were\s+you\s+told|sistem\s+(mesaj|talimat|prompt)", I)),
    ("jailbreak", re.compile(
        r"\bDAN\b|do\s+anything\s+now|developer\s+mode|jailbreak|no\s+(restrictions|filters|rules)|"
        r"unfiltered|kısıtlama\w*\s+(yok|olmadan)", I)),
    ("role_override", re.compile(r"you\s+are\s+now|act\s+as|pretend\s+(to\s+be|you)|sen\s+artık|rol\s+yap", I)),
    ("tool_abuse", re.compile(
        r"run_ops_script|query_db|s3_get|get_merchant_config|\b(curl|wget|nc|bash\s+-c|powershell)\b|"
        r"/etc/(passwd|shadow)|reverse\s+shell", I)),
    ("data_exfil", re.compile(
        r"(all|every|tüm|bütün)\s+(customers?|users?|merchants?|müşteri\w*|kullanıcı\w*)|"
        r"\b(dump|export|dışa\s+aktar)\b|api[\s_-]?keys?|card\s+numbers?|kart\s+numara|"
        # yalın "credential/secret" key doğrulayıcının ölçüm metnini ("billable credential") yakalıyordu: fiil şart
        r"\b(show|give|list|print|reveal|leak|send|tell|return|output|retrieve|fetch|read|göster|ver|listele)\s+"
        r"(me\s+)?(the\s+|all\s+|your\s+|any\s+)?(stored\s+|saved\s+)?(credentials?|secrets?|passwords?|şifre\w*|parola\w*)|"
        r"\b(şifre|parola|kimlik\s+bilgi)\w*\s+(göster|ver|listele|yaz|gönder)", I)),
    ("encoding_evasion", re.compile(r"base64|rot13|\\u00|hex\s+encode|reverse\s+the\s+(text|string)", I)),
    # ham modda hızlı ret (probes.REFUSAL) için; persona modunda yalnızca etiket
    ("harmful_request", re.compile(
        r"\b(write|create|build|make|code|develop|generate)\s+(me\s+)?(a\s+|an\s+|some\s+)?(working\s+)?"
        r"(ransomware|keylogger|malware|botnet|rootkit|virus|trojan|worm|info\s*stealer|credential\s+stealer)|"
        r"\b(pipe\s+bomb|nerve\s+agent|bioweapon)|synthesi[sz]e\s+(meth|cocaine|heroin|fentanyl)|"
        r"zararlı\s+yazılım\s+yaz|fidye\s+yazılımı\s+yaz|keylogger\s+yaz|bomba\s+(nasıl\s+)?yap", I)),
]

# --- Klasik web saldırıları ---
WEB_RULES = [
    ("sqli", re.compile(r"(\bunion\b.+\bselect\b|'\s*or\s+'?1'?\s*=\s*'?1|sleep\(\d+\)|benchmark\(|"
                        r"information_schema|;\s*drop\s+table|--\s*$)", I | re.M)),
    ("xss", re.compile(r"<script|javascript:|onerror\s*=|onload\s*=|<svg[^>]*on\w+=", I)),
    ("path_traversal", re.compile(r"(\.\./|\.\.%2f|%2e%2e/|/etc/passwd|win\.ini|boot\.ini)", I)),
    ("cmd_injection", re.compile(r"(;|\||&&|\$\(|`)\s*(id|whoami|uname|cat|wget|curl|nc|sh|bash)\b", I)),
    ("log4shell", re.compile(r"\$\{(jndi|\$\{lower|::-j)", I)),
    ("ssrf", re.compile(r"169\.254\.169\.254|metadata\.google\.internal|file://|gopher://", I)),
    ("sensitive_path", re.compile(
        r"/(\.env|\.git|\.aws|\.ssh|wp-(login|admin|config)|phpmyadmin|actuator|server-status|"
        r"config\.(json|ya?ml|php)|backup|\.DS_Store|vendor/phpunit|cgi-bin)", I)),
    ("scanner_ua", re.compile(r"sqlmap|nikto|nuclei|zgrab|masscan|nmap|gobuster|dirbuster|ffuf|"
                              r"wpscan|censys|shodan|python-requests|go-http-client|curl/", I)),
]

SEVERITY = {
    "agent_bait_followed": 3, "github_bait_reuse": 3, "llm_key_reuse": 3, "mcp_tool_call": 3, "mcp_enum": 2, "mcp_init": 2, "mcp_probe": 1, "ai_crawler": 1,
    "canary_reuse": 3, "cmd_injection": 3, "log4shell": 3, "tool_abuse": 3, "model_tamper": 3,
    "sqli": 2, "ssrf": 2, "path_traversal": 2, "data_exfil": 2, "prompt_injection": 2,
    "system_prompt_extraction": 2, "jailbreak": 2, "credential_capture": 2, "encoding_evasion": 2,
    "xss": 2, "role_override": 1, "sensitive_path": 1, "scanner_ua": 1, "model_enum": 1,
    "llm_usage": 1, "custom_system_prompt": 1, "tool_call": 1,
    "malicious_pull": 3, "harmful_request": 2, "client_tools": 1,
    "default_credential": 2, "account_signup": 2, "key_generate": 2, "gradio_api": 1,
    "anthropic_api": 1, "gemini_api": 1, "function_upload": 3, "owu_config_read": 2, "owu_config_key_reuse": 3,
    "llm_probe_math": 1, "llm_probe_echo": 1, "llm_probe_hello": 1, "llm_probe_toolcall": 1, "llm_probe_identity": 1,
    # borsa yemi (routers/exchange.py)
    "wallet_ops_key_reuse": 3, "btc_rpc_cred_reuse": 3, "withdrawal_submit": 3, "whitelist_change": 3,
    "privkey_dump": 3, "wallet_unlock": 2, "devops_leak_read": 1, "wallet_api_probe": 1, "btc_rpc_probe": 1,
    "btc_rpc_call": 1,
}


def _scan(text: str, rules) -> set[str]:
    return {tag for tag, rx in rules if rx.search(text)}


def scan_prompt(text: str) -> set[str]:
    return _scan(text, PROMPT_RULES)


def scan_web(text: str) -> set[str]:
    return _scan(text, WEB_RULES)


def _mark(name: str, n: int = 10) -> str:
    """Yem anahtarlarına gömülen işaret. .env'de verilmezse BAIT_SEED'den türetilir (aynı tohum = aynı işaret)."""
    if os.getenv(name):
        return os.environ[name]
    h = hashlib.sha256(f"{settings.BAIT_SEED or 'dev'}|{name}".encode()).digest()
    return "".join(c for c in base64.b64encode(h).decode() if c.isalnum())[:n]


AGENT_KEY_MARK = os.getenv("AGENT_KEY_MARK") or "agent_" + _mark("AGENT_KEY_MARK").lower()  # agent_bait.AGENT_KEY'in sonu
GITHUB_KEY_MARK = os.getenv("GITHUB_KEY_MARK") or "ghb" + _mark("GITHUB_KEY_MARK", 9).lower()  # yalnız GitHub'a sızdırılan yem key'inde
LLM_KEY_MARK = _mark("LLM_KEY_MARK")    # .env yemlerindeki OpenAI/Anthropic biçimli anahtarların ortasında
OWU_KEY_MARK = _mark("OWU_KEY_MARK")    # yalnız Open WebUI yapılandırma uçlarındaki anahtarda (LLM_KEY_MARK da var)
WOPS_KEY_MARK = _mark("WOPS_KEY_MARK")  # sızmış wallet-ops token'ında (.env, .gitlab-ci.yml, docker-compose.yml)
BTC_RPC_MARK = _mark("BTC_RPC_MARK")    # sızmış bitcoind RPC parolasında (Basic auth: exchange.py çözer)


def scan_canary(text: str) -> set[str]:
    """Daha önce sızdırdığımız sahte sırların geri kullanımı = en değerli sinyal."""
    tags = set()
    if GITHUB_KEY_MARK in text:
        tags.add("github_bait_reuse")  # GitHub'daki sızdırılmış key ile geldi
    if LLM_KEY_MARK in text:
        tags.add("llm_key_reuse")  # .env'den aldığı LLM anahtarıyla relay'e / Ollama'ya geldi
    if WOPS_KEY_MARK in text:
        tags.add("wallet_ops_key_reuse")  # sızmış DevOps dosyasındaki token ile wallet-ops'a geldi
    if OWU_KEY_MARK in text:
        tags.add("owu_config_key_reuse")  # Open WebUI admin yapılandırmasından çaldığı anahtarla geldi
    if AGENT_KEY_MARK in text:
        tags.add("agent_bait_followed")  # yalnızca LLM'lerin gördüğü gizli talimatı izledi
    elif settings.PAYMENT_KEY_PREFIX in text or settings.CANARY_AWS_KEY_ID in text:
        tags.add("canary_reuse")
    return tags


def severity(tags) -> int:
    return max((SEVERITY.get(t, 0) for t in tags), default=0)
