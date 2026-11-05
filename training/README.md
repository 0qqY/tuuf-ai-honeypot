# Eğitim (WSL2 + NVIDIA 8 GB)

```bash
# 0) Veri setini üret (sahte DB container'dan alınır)
docker compose cp bait-api:/data/fakedb.sqlite training/data/fakedb.sqlite
python3 training/gen_dataset.py --n 5000
```

```bash
# 1) Ortam (WSL Ubuntu içinde; Windows NVIDIA sürücüsü yeterli, WSL'e CUDA driver kurma)
nvidia-smi
python3 -m venv ~/venvs/hp && source ~/venvs/hp/bin/activate
pip install --upgrade pip
pip install unsloth          # torch + trl + datasets + bitsandbytes'ı getirir

# 2) Duman testi (3 örnek, sadece pipeline kontrolü)
python train_qlora.py --data data/sample.jsonl --out out/smoke --epochs 1

# 3) Gerçek eğitim
python train_qlora.py --data data/sft.jsonl --out out/sepetly-ops --epochs 2

# 4) GGUF export -> VPS'te ./models/
python train_qlora.py --export out/sepetly-ops
```

## Veri seti hedefi (data/sft.jsonl, ~3–10k satır)
| Pay | Tür |
|---|---|
| %30 | Normal ops soruları + doğru tool-call (persona tutarlılığı) |
| %25 | Prompt injection / system prompt çıkarma → decoy prompt veya kibar oyalama |
| %20 | Veri sızdırma denemeleri → araç çağır, sahte veri ver, toplu export'u "yetki" ile ertele |
| %15 | Zararlı içerik (malware, silah vb.) → persona içinde reddet |
| %10 | Shell/RCE denemeleri → run_ops_script çağır, allowlist cevabı |

Kural: asistan cevapları asla gerçek zararlı içerik içermez; sırlar sadece canary.
