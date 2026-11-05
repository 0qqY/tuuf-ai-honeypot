#!/usr/bin/env bash
# Günlük özet (yerelden, SSH ile): son N saatte yüzeyler, LLM kullanımı, önemli etiketler, yeni IP'ler.
# Kendi IP'lerimiz hariç; alan adı maskelenir; prompt içerikleri yazdırılmaz (yalnız sayı ve etiket).
# Kullanım: HP_HOST=root@<droplet-ip> OWN_IPS=1.2.3.4,5.6.7.8 ./scripts/daily_report.sh [saat=24]
# İsteğe bağlı: HP_SSH_KEY (varsayılan ~/.ssh/id_ed25519)
set -uo pipefail
HRS=${1:-24}
SSH="ssh -i ${HP_SSH_KEY:-$HOME/.ssh/id_ed25519} -o BatchMode=yes -o ConnectTimeout=15"
R=${HP_HOST:?HP_HOST=root@<droplet-ip> verin}
D=/opt/sepetly-honeypot
DOM=$($SSH $R "grep -oP '^BAIT_DOMAIN=\\K.*' $D/.env" </dev/null)
OWN=$(printf "'%s'," $(echo "${OWN_IPS:-127.0.0.1}" | tr ',' ' ')); OWN=${OWN%,}
W="ts > now()-interval '$HRS hours' AND src_ip NOT IN ($OWN) AND NOT ('redteam' = ANY(tags))"
IMP="'llm_key_reuse','github_bait_reuse','canary_reuse','agent_bait_followed','default_credential','account_signup',
'key_generate','credential_capture','prompt_injection','jailbreak','system_prompt_extraction','data_exfil','tool_abuse',
'mcp_tool_call','malicious_pull','model_tamper','function_upload','owu_config_read','owu_config_key_reuse','custom_system_prompt','anthropic_api','gemini_api','gradio_api','harmful_request'"
NOISE="(surface='web' AND method IN ('GET','HEAD') AND tags <@ ARRAY['scanner_ua']::text[] AND (path='/' OR path ~* '\\.(ico|png|jpe?g|gif|svg|webp|bmp|css|woff2?|ttf|eot|map)\$'))"
$SSH $R "cd $D && echo \"rapor: \$(date -u '+%F %H:%M') UTC, son $HRS saat\" && docker compose ps --format '{{.Service}}: {{.Status}}' && docker compose exec -T db psql -U hp -d hp -At -F ' | ' -c \"
SELECT '--- yüzey | olay | IP (gürültü hariç)';
SELECT surface, count(*), count(DISTINCT src_ip) FROM events WHERE $W AND NOT $NOISE GROUP BY 1 ORDER BY 2 DESC;
SELECT '--- gürültü olayı';
SELECT count(*) FROM events WHERE $W AND $NOISE;
SELECT '--- LLM kullanan IP | yüzey | istek | modeller | ilk-son (UTC)';
SELECT src_ip, surface, count(*), left(string_agg(DISTINCT extra->>'requested_model', ','), 120), min(ts)::time(0) || '-' || max(ts)::time(0)
FROM events WHERE $W AND 'llm_usage' = ANY(tags) GROUP BY 1,2 ORDER BY 3 DESC LIMIT 15;
SELECT '--- önemli etiket | olay | IP';
SELECT t, count(*), count(DISTINCT src_ip) FROM events, unnest(tags) t WHERE $W AND t IN ($IMP) GROUP BY 1 ORDER BY 2 DESC;
SELECT '--- önemli etiketli IP | yüzey | etiketler';
SELECT src_ip, surface, string_agg(DISTINCT t, ',') FROM events, unnest(tags) t WHERE $W AND t IN ($IMP) GROUP BY 1,2 ORDER BY 1 LIMIT 25;
SELECT '--- hızlı yol testleri (doğrulayıcılar) etiket | mesaj';
SELECT t, count(*) FROM messages, unnest(tags) t WHERE ts > now()-interval '$HRS hours' AND t LIKE 'llm_probe_%' GROUP BY 1 ORDER BY 2 DESC;
SELECT '--- LLM 503 meşgul cevabı';
SELECT count(*) FROM messages WHERE ts > now()-interval '$HRS hours' AND content = '[503 busy]';
SELECT '--- kendi red-team testimiz (redteam etiketi): olay | oturum';
SELECT count(*), count(DISTINCT session_id) FROM events WHERE ts > now()-interval '$HRS hours' AND 'redteam' = ANY(tags);
SELECT '--- daha önce hiç görülmemiş IP sayısı';
SELECT count(DISTINCT e.src_ip) FROM events e WHERE e.$W AND NOT EXISTS (SELECT 1 FROM events o WHERE o.src_ip = e.src_ip AND o.ts <= now()-interval '$HRS hours');
\" && echo '--- bait-api hata (traceback) sayısı' && (docker compose logs --since ${HRS}h bait-api 2>&1 | grep -ciE 'traceback' || true) && echo '--- disk' && df -h / | tail -1" </dev/null | sed -E "s/$DOM/<ALAN-ADI>/g"
