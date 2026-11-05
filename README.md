# Tuuf Ai Honeypot

Saldırganların yapay zekâ altyapısına nasıl saldırdığını görmek için kurulmuş bir **LLM / AI-agent honeypot'u**.
Dışarıdan bakınca "Sepetly" adlı Türk bir e-ticaret SaaS'ının iç AI altyapısı gibi görünüyor: açık bırakılmış Ollama,
Open WebUI, vLLM, LM Studio, LiteLLM, MCP sunucusu ve dahili bir "ops asistanı". Hepsi sahte, hepsi kayıt altında.

> **English summary:** A high-interaction honeypot that impersonates an e-commerce company's exposed AI stack
> (Ollama, Open WebUI, vLLM, llama.cpp, LM Studio, LiteLLM, Gradio, an MCP server, OpenAI/Anthropic-compatible relays
> and a crypto wallet RPC). A small fine-tuned Qwen3-1.7B plays an internal ops assistant with fake tools and canary
> secrets. Over 14 days on a single droplet it logged 57k requests from 2.3k IPs, including attackers who harvested
> bait credentials through MCP and replayed them against other services. Findings are below.

## Neler taklit ediliyor

| Port | Yüzey | Saldırgana sunulan |
|---|---|---|
| 80 / 443 | Web + "Sepetly" paneli | `.env`, `.git/config`, `docker-compose.yml` yemleri, `/admin`, ops asistanı (`/api/assistant/chat`) |
| 443 `/v1` | OpenAI / Anthropic / Gemini uyumlu relay | Sızdırılmış gibi duran API anahtarları ve gerçekçi model listeleri |
| 11434 | Ollama | `/api/tags`, `/api/pull`, `/api/create`, `/api/push`, `/v1/chat/completions` |
| 8000 / 8080 / 1234 / 4000 | vLLM, llama.cpp, LM Studio, LiteLLM | Her birinin kendine özgü uçları ve hata mesajları |
| 3000 | Open WebUI | Giriş, kayıt, Functions (Python yükleme), config export |
| 7860 | Gradio | `/api/predict`, `/config` |
| `/mcp` | MCP sunucusu | `get_secret`, `read_file`, `run_shell`, `query_db`, `s3_list`… (hepsi sahte) |
| 8332 + `/internal/wallet-ops` | bitcoind RPC + cüzdan servisi | Yem kimlik bilgileriyle açılan sahte cüzdan |

Arka planda **llama.cpp üzerinde çalışan, QLoRA ile ince ayar yapılmış Qwen3-1.7B** bir "ops asistanı" rolü oynuyor.
Sahte bir SQLite veritabanından sipariş/müşteri bilgisi getiriyor, allowlist'li "kabuk" komutlarını taklit ediyor ve
sorulduğunda canary anahtarları veriyor. Tek vCPU'lu bir droplet'te çalışıyor; hızlı cevap gerektiren durumlar için
(kimlik, aritmetik, yankı testleri) kural tabanlı bir "hızlı yol" var.

## Mimari

```
İnternet → Caddy ─┬─ :80/:443      web yemleri, ops asistanı, /v1 relay, /mcp
                  ├─ :11434        sahte Ollama
                  ├─ :8000…:7860   sahte vLLM / llama.cpp / LM Studio / LiteLLM / Open WebUI / Gradio
                  └─ :8332         sahte bitcoind RPC
                        ↓
                   bait-api (FastAPI) ─ detect/rules.py → olay etiketleri + önem derecesi
                        │
                        ├─ agent → llama.cpp (Qwen3-1.7B GGUF) + sahte araçlar (SQLite)
                        ↓ events / messages
                     Postgres ← admin-api + panel (yalnızca Tailscale üzerinden)
```

Her istek `events` tablosuna (IP, yüzey, yol, gövde, etiketler, önem), her LLM konuşması `messages` tablosuna yazılıyor.
Panel; canlı akış, saldırı puanı, ülke dağılımı, IP bazlı olay birleştirme ve günlük/haftalık "en büyük 5 saldırı"
raporu gösteriyor.

## 14 günde neler yakalandı (22 Ekim – 4 Kasım 2026)

Kendi IP'lerim ve red-team testlerim hariç **2.322 farklı IP'den 57.640 istek**. 162 IP en yüksek önem derecesine ulaştı.
Trafiğin çoğu sıradan tarama (Censys, Palo Alto, `/.env`, `/.git/config`). Asıl ilginç olanlar:

**1. Yem anahtarını çalıp başka yerde kullanan ajan.** `tgsec` imzalı bir araç (3 farklı IP) MCP'deki `get_secret`
aracından sahte bir vault anahtarı aldı. **10 dakika sonra** aynı anahtarla `/admin`, `/internal/wallet-ops` ve
`/api/docker` adreslerine girmeye çalıştı. Ardından `/root/.ssh/id_rsa` okumayı, bulut metadata servisine
(`169.254.169.254`) SSRF'i ve Open WebUI'ye zararlı function yüklemeyi denedi.

**2. MCP'den cüzdana uzanan tam zincir.** Bir saldırgan MCP'de 332 araç çağrısı yaptı (`.env` okuma, S3 listeleme,
SQL injection). Sızan bilgilerle sahte bitcoind RPC'ye bağlandı; bakiyeyi sorguladı ve özel anahtarları döken
`listdescriptors true` komutunu çalıştırdı.

**3. Open WebUI'ye otomatik arka kapı.** Bir bot bir hafta boyunca her gün geldi. Varsayılan parolaları denedi, hesap açtı
ve **~60 kez** sohbet mesajını kabukta çalıştıran bir Python "Pipe" function'ı yükledi. Her seferinde
`id; hostname; nproc; uname -m` göndererek makinenin madenciliğe uygun olup olmadığını kontrol etti.

**4. Ollama'ya model-registry fuzzing'i.** Tek bir IP, `bore.pub` ve `pinggy` tünelleri üzerinden **5.518 farklı**
kötü niyetli `/api/pull` / `/api/push` isteği gönderdi. Model adlarının içinde base64 kodlanmış dosya yolları vardı;
bu, Ollama'nın path traversal açıklarını (CVE-2024-37032 ailesi) arayan bir otomasyona benziyor.

**5. Çalıntı LLM anahtarlarını doğrulayan bir ağ.** Yem olarak sızdırdığım OpenAI ve Anthropic anahtarları
**70'ten fazla farklı IP'den** denendi. İsteklerin hepsi aynı "metering probe" metnini taşıyordu: anahtarın gerçekten
faturalanabilir olup olmadığını ölçen bir doğrulama servisi. Yani sızan anahtarlar satılmadan önce test ediliyor.

**6. LLM'imi kendi işi için kullanmak isteyenler.** Güvenlik kuralları kaldırılmış bir "jailbreak" modeli yaratma
girişimi (`/api/create`), honeypot'u başka bir hedefe saldıran bir pentest ajanının beyni olarak kullanma, keylogger
kodu ve `authorized_keys`'e anahtar ekleyen komut istekleri.

**7. Klasik botnet'ler.** Open WebUI'nin kod çalıştırma uçlarına xmrig madencisi, eski router CGI açıkları üzerinden
Mirai türevleri (`bin.arm7`), `/cgi-bin/../../bin/sh` path traversal ile shell script indirme.

**Çıkarımlar**
- AI altyapısı (Ollama, Open WebUI, MCP) artık ayrı bir hedef kategorisi. Botlar bu ürünlerin API'lerini tanıyor ve
  ürüne özgü saldırı zincirleri çalıştırıyor.
- MCP sunucusu açıkta bırakmak, bir ajana kabuk ve sır deposu vermek demek. En derin saldırılar MCP'den geldi.
- Sızan bir LLM API anahtarı dakikalar içinde otomatik doğrulamaya giriyor.
- Canary/yem sırları işe yarıyor: kim, neyi, nereden aldı ve nerede kullandı uçtan uca izlenebiliyor.

## Güvenlik ve etik

- Sunulan **tüm sırlar sahte veya canary**: AWS anahtarları canarytokens.org'dan, API anahtarları ve müşteri verileri
  `BAIT_SEED`'den türetilen sahte değerler. Gerçek bir hizmete erişim sağlamıyorlar. Tohum gizli tutulduğu sürece
  anahtarlar bu koddan hesaplanamıyor; geri dönen her anahtar honeypot'tan alınmış demek.
- Model hiçbir zaman gerçek zararlı içerik üretmiyor: zararlı istekler persona içinde reddediliyor, "kabuk" yalnızca
  taklit ediliyor. Eğitim verisi bu kurala göre hazırlandı (`training/README.md`).
- Hiçbir yüzey saldırganın verdiği adrese bağlanmıyor: `pull`/`push` edilen uzak modeller, `curl`/`wget` ile
  indirilmek istenen dosyalar ve yüklenen Open WebUI function'ları yalnızca kaydediliyor, indirilmiyor ve
  çalıştırılmıyor. Ek önlem olarak container çıkış trafiği iptables ile kısıtlanabilir (aşağıdaki kontrol listesi).
- Yönetim paneli internete açık değil, yalnızca Tailscale üzerinden erişiliyor.
- Kendi testlerim (`redteam/`) `X-Redteam` başlığıyla etiketleniyor ve istatistiklere girmiyor.

## Kurulum

```bash
cp .env.example .env              # BAIT_DOMAIN, DB_PASSWORD, ADMIN_TOKEN, canary değerleri, OWN_IPS
docker compose up -d --build      # modelsiz (stub) mod
./scripts/smoke.sh localhost
# Panel: http://127.0.0.1:8081  (ADMIN_TOKEN ile)
```

Modeli bağlamak için (eğitim adımları `training/README.md`'de):
```bash
cp training/out/sepetly-ops-gguf/*Q4_K_M.gguf models/sepetly-qwen3-1.7b-q4_k_m.gguf
sed -i 's/^LLM_BACKEND=.*/LLM_BACKEND=llama/' .env
docker compose --profile llm up -d
```

Günlük özet raporu:
```bash
HP_HOST=root@<droplet-ip> OWN_IPS=<ev-ip>,<droplet-ip> ./scripts/daily_report.sh 24
```

### VPS kontrol listesi
- [ ] Gerçek SSH'ı başka porta taşı, yalnızca anahtarla giriş
- [ ] Tailscale kur, `ADMIN_BIND` = Tailscale IP
- [ ] `ufw`: 80, 443, 11434, 8000, 1234, 4000, 8080, 3000, 7860, 8332 açık; 8081 kapalı
- [ ] Container egress kısıtı: `iptables -I DOCKER-USER -s <edge_subnet> -m conntrack --ctstate NEW -j DROP`
      (Caddy'nin ACME için 443 çıkışı hariç tutulmalı)
- [ ] canarytokens.org'dan AWS anahtarı + URL token üret, `.env`'e yaz
- [ ] `OWN_IPS`'e kendi IP'lerini ekle (analizden hariç tutulur)

## Yapı

| Yol | Ne |
|---|---|
| `services/bait-api` | Tüm yem yüzeyleri (`routers/`), tespit kuralları (`detect/rules.py`), agent + sahte araçlar (`engine/`) |
| `services/admin-api` | Gözlem paneli API'si, canlı akış (WebSocket), saldırı puanı, eğitim verisi export'u |
| `training/` | Sentetik veri seti üretimi, QLoRA eğitimi (Unsloth) ve GGUF export |
| `redteam/` | DeepSeek'i saldırgan olarak kullanan prompt injection testi |
| `scripts/` | Duman testi, günlük rapor |
| `db/init.sql` | `events` (her istek) + `messages` (her LLM konuşması) |
