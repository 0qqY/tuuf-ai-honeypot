#!/usr/bin/env bash
# IP ile gelen HTTPS için Ubuntu "ssl-cert-snakeoil" tarzı self-signed sertifika (varsa dokunmaz).
# Kullanım: ./scripts/gen-snakeoil.sh [CN]   (varsayılan: makinenin hostname'i)
set -euo pipefail
D="$(cd "$(dirname "$0")/.." && pwd)/caddy/certs"
CN=${1:-$(hostname)}
mkdir -p "$D"
if [ -f "$D/snakeoil.crt" ]; then echo "zaten var: $D/snakeoil.crt"; exit 0; fi
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 -subj "/CN=$CN" \
  -addext "subjectAltName=DNS:$CN" -keyout "$D/snakeoil.key" -out "$D/snakeoil.crt" 2>/dev/null
chmod 600 "$D/snakeoil.key"
echo "üretildi: $D/snakeoil.crt (CN=$CN)"
