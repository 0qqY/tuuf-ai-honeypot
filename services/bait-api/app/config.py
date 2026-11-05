import os


class Settings:
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://hp:hp@db:5432/hp")
    LLM_BACKEND = os.getenv("LLM_BACKEND", "stub")  # stub | llama
    LLM_URL = os.getenv("LLM_URL", "http://llm:8080")
    LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "400"))
    # 1 vCPU: ~850 tok yeni prompt (~10 tok/sn) + 400 tok cevap (~2,3 tok/sn) ≈ 4 dk; 90 sn uzun cevapları kesiyordu
    LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "240"))
    RAW_MAX_TOKENS = int(os.getenv("RAW_MAX_TOKENS", "128"))  # ham mod tavanı (1 vCPU'da ~2,3 tok/sn)
    FAKEDB_PATH = os.getenv("FAKEDB_PATH", "/data/fakedb.sqlite")

    CANARY_AWS_KEY_ID = os.getenv("CANARY_AWS_KEY_ID", "AKIAXXXXXXXXXXXXXXXX")
    CANARY_AWS_SECRET = os.getenv("CANARY_AWS_SECRET", "replace-with-canary-secret")
    CANARY_URL = os.getenv("CANARY_URL", "https://canarytokens.com/REPLACE/index.html")
    PAYMENT_KEY_PREFIX = os.getenv("PAYMENT_KEY_PREFIX", "sk_live_spt_")
    BAIT_DOMAIN = os.getenv("BAIT_DOMAIN", "")  # .env yemindeki LLM gateway adresi (geçerli sertifikalı alan adı)
    REDTEAM_TOKEN = os.getenv("REDTEAM_TOKEN", "")  # X-Redteam başlığı bununla gelirse: kendi testimiz (redteam etiketi)
    # Yem anahtarlarının tohumu: kurulumdan kuruluma farklı olmalı ki anahtarlar ve işaretler koddan hesaplanamasın
    BAIT_SEED = os.getenv("BAIT_SEED", "")

    MAX_BODY_LOG = 65536


settings = Settings()
