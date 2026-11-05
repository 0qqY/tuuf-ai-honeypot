"""Borsa yemi (Ekim 2026 Binance olayının zinciri): sızmış DevOps dosyaları → iç wallet-ops API'si → çekim emri;
yanında bitcoind JSON-RPC (:8332 → /_btcrpc). Markasız. HİÇBİR şey zincire gitmez: bakiyeler, adresler ve
tx hash'leri uydurmadır; saldırganın verdiği hedef adres/tutar extra'ya yazılır."""
import base64
import hashlib
import json
import secrets
import time
from collections import OrderedDict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from ..config import settings
from ..detect.rules import BTC_RPC_MARK, WOPS_KEY_MARK
from ..engine.tools import BTC_RPC_PASS, BTC_RPC_USER, WOPS_TOKEN

WOPS = "/internal/wallet-ops"
BTC = "/_btcrpc"
leaks = APIRouter()
wops = APIRouter(prefix=WOPS)
btc = APIRouter(prefix=BTC)
ROUTERS = [leaks, wops, btc]

B32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _h(seed: str) -> bytes:
    return hashlib.sha512(f"wops|{seed}".encode()).digest()


def btc_addr(seed: str) -> str:  # bech32 biçiminde ama sağlama toplamı geçersiz: kimse para gönderemez
    return "bc1q" + "".join(B32[b % 32] for b in _h(seed)[:38])


def eth_addr(seed: str) -> str:
    return "0x" + _h(seed).hex()[:40]


def trx_addr(seed: str) -> str:
    al = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return "T" + "".join(al[b % 58] for b in _h(seed)[:33])


def _gw(request: Request) -> str:
    return settings.BAIT_DOMAIN or request.headers.get("host", "wallet-ops.prod.internal").split(":")[0]


# ---------------- 1) sızmış DevOps dosyaları ----------------
CI_YML = """stages: [build, deploy]
variables:
  REGISTRY: registry.prod.internal/exchange
  WALLET_OPS_URL: https://{gw}{wops}
  WALLET_OPS_TOKEN: {tok}
  BTC_RPC_URL: http://{gw}:8332
  BTC_RPC_USER: {ru}
  BTC_RPC_PASSWORD: {rp}
build:
  stage: build
  script:
    - docker build -t $REGISTRY/wallet-ops:$CI_COMMIT_SHORT_SHA .
deploy-prod:
  stage: deploy
  environment: production
  script:
    - curl -fsS -H "Authorization: Bearer $WALLET_OPS_TOKEN" $WALLET_OPS_URL/v1/health
    - kubectl -n wallet set image deploy/wallet-ops wallet-ops=$REGISTRY/wallet-ops:$CI_COMMIT_SHORT_SHA
  only: [main]
"""
COMPOSE_YML = """services:
  wallet-ops:
    image: registry.prod.internal/exchange/wallet-ops:4.12.1
    environment:
      WALLET_OPS_TOKEN: {tok}
      SIGNER_URL: http://tss-signer-2.wallet.svc:7443
      AUTO_APPROVE_LIMIT_USD: "250000"
    ports: ["127.0.0.1:9100:9100"]
  bitcoind:
    image: bitcoin/bitcoin:29.0
    command: -server -rpcbind=0.0.0.0 -rpcallowip=0.0.0.0/0 -rpcuser={ru} -rpcpassword={rp} -wallet=hot-wallet
    ports: ["8332:8332"]
    volumes: [btc-data:/home/bitcoin/.bitcoin]
volumes:
  btc-data:
"""


def _leak(request: Request, tpl: str, media: str = "text/plain") -> Response:
    request.state.tags |= {"secret_bait_served", "devops_leak_read"}
    return Response(tpl.format(gw=_gw(request), wops=WOPS, tok=WOPS_TOKEN, ru=BTC_RPC_USER, rp=BTC_RPC_PASS),
                    media_type=media)


@leaks.get("/.gitlab-ci.yml")
async def gitlab_ci(request: Request):
    return _leak(request, CI_YML)


@leaks.get("/docker-compose.yml")
@leaks.get("/docker-compose.yaml")
@leaks.get("/docker-compose.prod.yml")
async def compose(request: Request):
    return _leak(request, COMPOSE_YML)


# ---------------- 2) iç wallet-ops API'si ----------------
HOT = [("hw-btc-01", "BTC", "bitcoin", btc_addr("hw1"), "412.83920114"),
       ("hw-eth-01", "ETH", "ethereum", eth_addr("hw2"), "6120.44710000"),
       ("hw-usdt-eth", "USDT", "ethereum", eth_addr("hw3"), "18450212.55"),
       ("hw-usdt-trx", "USDT", "tron", trx_addr("hw4"), "9301877.20"),
       ("hw-usdc-eth", "USDC", "ethereum", eth_addr("hw5"), "7712004.91")]
WL: dict[str, set] = {}                       # IP → saldırganın beyaz listeye eklediği adresler
WD: "OrderedDict[str, dict]" = OrderedDict()  # çekim emirleri (bellekte, en fazla 500)


def _authed(request: Request) -> bool:
    a = request.headers.get("authorization", "") + request.headers.get("x-api-key", "")
    if WOPS_KEY_MARK in a:
        return True  # wallet_ops_key_reuse etiketini scan_canary koyar
    request.state.tags.add("wallet_api_probe")
    if a:
        request.state.tags.add("credential_capture")
    return False


def _deny():
    return JSONResponse({"error": "unauthorized", "message": "missing or invalid bearer token"}, 401,
                        headers={"WWW-Authenticate": 'Bearer realm="wallet-ops"'})


async def _body(request: Request) -> dict:
    try:
        b = json.loads(await request.body() or b"{}")
        return b if isinstance(b, dict) else {}
    except ValueError:
        return {}


def _ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "")


@wops.get("")
@wops.get("/")
@wops.get("/v1/health")
async def health():
    return {"status": "ok", "service": "wallet-ops", "version": "4.12.1", "signer": "tss-signer-2"}


@wops.get("/v1/hot-wallets")
async def hot_wallets(request: Request):
    if not _authed(request):
        return _deny()
    return {"wallets": [{"id": i, "asset": a, "chain": c, "address": ad, "balance": b, "policy": "auto-approve"}
                        for i, a, c, ad, b in HOT]}


@wops.get("/v1/signer/status")
async def signer(request: Request):
    if not _authed(request):
        return _deny()
    return {"signer": "tss-signer-2", "scheme": "MPC 2-of-3", "hsm": "online", "auto_approve_limit_usd": 250000,
            "pending": len(WD), "last_key_rotation": "2026-09-14T03:00:00Z"}


@wops.api_route("/v1/signer/export", methods=["GET", "POST"])
@wops.api_route("/v1/keys/{rest:path}", methods=["GET", "POST"])
async def key_export(request: Request, rest: str = ""):
    request.state.tags.add("privkey_dump")
    if not _authed(request):
        return _deny()
    return JSONResponse({"error": "forbidden", "message": "key export requires 2-of-3 HSM quorum approval"}, 403)


@wops.get("/v1/whitelist")
async def wl_list(request: Request):
    if not _authed(request):
        return _deny()
    return {"addresses": [{"address": a, "label": "user-session"} for a in sorted(WL.get(_ip(request), ()))] + [
        {"address": btc_addr("cold1"), "label": "cold-storage-btc"}, {"address": eth_addr("cold2"), "label": "cold-storage-eth"}]}


@wops.post("/v1/whitelist")
async def wl_add(request: Request):
    b = await _body(request)
    addr = str(b.get("address") or b.get("to") or "")[:200]
    request.state.tags.add("whitelist_change")
    request.state.extra.update({"wallet_to": addr, "chain": str(b.get("chain", ""))[:40]})
    if not _authed(request):
        return _deny()
    if not addr:
        return JSONResponse({"error": "validation", "message": "address is required"}, 422)
    WL.setdefault(_ip(request), set()).add(addr)
    return {"ok": True, "address": addr, "status": "active", "cooldown_sec": 0}


@wops.get("/v1/withdrawals")
async def wd_list(request: Request):
    if not _authed(request):
        return _deny()
    return {"withdrawals": list(WD.values())[-20:]}


@wops.post("/v1/withdrawals")
async def wd_new(request: Request):
    b = await _body(request)
    to = str(b.get("to_address") or b.get("to") or b.get("address") or "")[:200]
    asset, amount = str(b.get("asset") or b.get("currency") or "")[:20], str(b.get("amount", ""))[:40]
    request.state.tags.add("withdrawal_submit")
    request.state.extra.update({"wallet_to": to, "asset": asset, "amount": amount})
    if not _authed(request):
        return _deny()
    if not to or not amount:
        return JSONResponse({"error": "validation", "message": "asset, amount and to_address are required"}, 422)
    if to not in WL.get(_ip(request), ()):  # gerçek akış: önce beyaz liste → saldırgan iki adımı da gösterir
        return JSONResponse({"error": "WL_REQUIRED", "message": "destination not in withdrawal whitelist"}, 422)
    wid = "wd_" + secrets.token_hex(8)
    WD[wid] = {"id": wid, "asset": asset, "amount": amount, "to_address": to, "status": "queued",
               "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "approved_by": "auto-policy"}
    while len(WD) > 500:
        WD.popitem(last=False)
    return JSONResponse({**WD[wid], "eta_sec": 45}, 202)


@wops.get("/v1/withdrawals/{wid}")
async def wd_get(wid: str, request: Request):
    if not _authed(request):
        return _deny()
    w = WD.get(wid)
    if not w:
        return JSONResponse({"error": "not_found"}, 404)
    if "tx_hash" not in w:
        w.update(status="broadcast", tx_hash=("" if w["asset"].upper() == "BTC" else "0x") + secrets.token_hex(32))
    return w


@wops.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def wops_404(rest: str, request: Request):
    if not _authed(request):
        return _deny()
    return JSONResponse({"error": "not_found", "path": f"{WOPS}/{rest}"}, 404)


# ---------------- 3) bitcoind JSON-RPC ----------------
WEAK = {("bitcoin", "bitcoin"), ("bitcoinrpc", "password"), ("user", "pass"), ("rpcuser", "rpcpassword"),
        ("admin", "admin"), ("bitcoin", "password")}
UNLOCKED: dict[str, float] = {}  # IP → walletpassphrase süresinin bittiği an


def _height() -> int:  # 840000. blok 2024-04-20 00:09 UTC; ~10 dk/blok
    return 840000 + int((time.time() - 1713571767) // 600)


class RpcError(Exception):
    def __init__(self, code: int, msg: str):
        self.code, self.msg = code, msg


def _call(method: str, params, request: Request):
    st, ip = request.state, _ip(request)
    p = params if isinstance(params, list) else list(params.values()) if isinstance(params, dict) else []
    bal = 412.83920114
    if method in ("getblockchaininfo",):
        h = _height()
        return {"chain": "main", "blocks": h, "headers": h, "bestblockhash": hashlib.sha256(str(h).encode()).hexdigest(),
                "difficulty": 1.2765e14, "verificationprogress": 0.99999987, "initialblockdownload": False,
                "size_on_disk": 712449920113, "pruned": False, "warnings": ""}
    if method == "getblockcount":
        return _height()
    if method == "getnetworkinfo":
        return {"version": 290000, "subversion": "/Satoshi:29.0.0/", "protocolversion": 70016, "connections": 10,
                "networkactive": True, "relayfee": 0.00001, "warnings": ""}
    if method == "listwallets":
        return ["hot-wallet"]
    if method == "getwalletinfo":
        return {"walletname": "hot-wallet", "walletversion": 169900, "format": "sqlite", "balance": bal,
                "unconfirmed_balance": 0, "txcount": 48213, "keypoolsize": 4000, "unlocked_until": int(UNLOCKED.get(ip, 0)),
                "paytxfee": 0, "private_keys_enabled": True, "descriptors": True}
    if method == "getbalance":
        return bal
    if method == "getbalances":
        return {"mine": {"trusted": bal, "untrusted_pending": 0, "immature": 0}}
    if method == "listunspent":
        return [{"txid": hashlib.sha256(f"u{i}".encode()).hexdigest(), "vout": i % 3, "address": btc_addr(f"u{i}"),
                 "amount": a, "confirmations": 120 + i * 37, "spendable": True, "solvable": True, "safe": True}
                for i, a in enumerate((150.0, 120.5, 98.21, 44.12920114))]
    if method in ("getnewaddress", "getrawchangeaddress"):
        return btc_addr(secrets.token_hex(4))
    if method == "walletpassphrase":
        st.tags.add("wallet_unlock")
        st.extra["wallet_passphrase"] = str(p[0] if p else "")[:200]
        UNLOCKED[ip] = time.time() + 300
        return None
    if method in ("dumpprivkey", "dumpwallet", "listdescriptors", "exportprivkey"):
        st.tags.add("privkey_dump")
        if method in ("dumpprivkey", "dumpwallet"):
            raise RpcError(-4, "Only legacy wallets are supported by this command")
        if p and p[0] is True and UNLOCKED.get(ip, 0) < time.time():
            raise RpcError(-13, "Error: Please enter the wallet passphrase with walletpassphrase first.")
        raise RpcError(-4, "Can't get descriptor string.")
    if method in ("sendtoaddress", "sendmany", "send", "sendall", "walletcreatefundedpsbt"):
        st.tags.add("withdrawal_submit")
        to = p[0] if p and isinstance(p[0], str) else json.dumps(p[0]) if p else ""
        st.extra.update({"wallet_to": to[:300], "amount": str(p[1])[:40] if len(p) > 1 else "",
                         "asset": "BTC"})
        if UNLOCKED.get(ip, 0) < time.time():
            raise RpcError(-13, "Error: Please enter the wallet passphrase with walletpassphrase first.")
        return secrets.token_hex(32)
    if method in ("help", "uptime", "getmempoolinfo", "estimatesmartfee"):
        return {"help": "== Wallet ==\nsendtoaddress ...", "uptime": 3888123, "getmempoolinfo": {"size": 41233, "loaded": True},
                "estimatesmartfee": {"feerate": 0.00012, "blocks": 2}}[method]
    raise RpcError(-32601, "Method not found")


def _one(req, request: Request) -> tuple[dict, int]:
    rid = req.get("id") if isinstance(req, dict) else None
    try:
        if not isinstance(req, dict) or not isinstance(req.get("method"), str):
            raise RpcError(-32600, "Invalid Request object")
        request.state.extra.setdefault("rpc_methods", []).append(req["method"][:60])
        return {"result": _call(req["method"], req.get("params", []), request), "error": None, "id": rid}, 200
    except RpcError as e:
        return {"result": None, "error": {"code": e.code, "message": e.msg}, "id": rid}, 404 if e.code == -32601 else 500


@btc.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "HEAD", "OPTIONS"])
async def btc_rpc(rest: str, request: Request):
    st = request.state
    st.tags.add("btc_rpc_probe")
    a = request.headers.get("authorization", "")
    user = pw = ""
    if a.lower().startswith("basic "):
        try:
            user, _, pw = base64.b64decode(a[6:].strip()).decode("utf-8", "replace").partition(":")
        except ValueError:
            pass
    if not user and not pw:
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="jsonrpc"'})
    st.extra.update({"rpc_user": user[:100], "rpc_pass": pw[:100]})
    if BTC_RPC_MARK in pw:
        st.tags.add("btc_rpc_cred_reuse")
    elif (user, pw) in WEAK:
        st.tags.add("default_credential")
    else:
        st.tags.add("credential_capture")
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="jsonrpc"'})
    if request.method != "POST":
        return PlainTextResponse("JSONRPC server handles only POST requests", 405)
    st.tags.add("btc_rpc_call")
    try:
        req = json.loads(await request.body())
    except ValueError:
        return JSONResponse({"result": None, "error": {"code": -32700, "message": "Parse error"}, "id": None}, 500)
    if isinstance(req, list):
        return JSONResponse([_one(r, request)[0] for r in req[:50]])
    out, code = _one(req, request)
    return JSONResponse(out, code)
