#!/usr/bin/env python3
"""
TitanX Testnet Bot v3 — MULTI AKUN
==================================
Fungsi trading & signing PERSIS SAMA dengan v2 (Ed25519 session signing,
long/short random, size 75% saldo). Yang baru:
  • Multi akun: tiap akun punya token + session sendiri, jalan paralel.
  • Tampilan console berwarna: banner, log per akun, tabel status, bar jeda.

Instalasi:
    pip install requests cryptography

Konfigurasi akun — accounts.json sebelah script (prioritas):
    [
      {"name": "akun1", "token": "eyJ..."},
      {"name": "akun2", "token": "eyJ...", "size_pct": 0.5}
    ]
Atau cara lama tetap jalan: .env berisi TITANX_TOKEN=eyJ...  (1 akun, nama "default")
Multi token di .env juga bisa: TITANX_TOKEN_1=..., TITANX_TOKEN_2=...

Perintah:
    python titanx_bot3.py status     # tabel saldo & posisi semua akun
    python titanx_bot3.py probe      # tes koneksi semua akun
    python titanx_bot3.py dry        # simulasi siklus semua akun
    python titanx_bot3.py run        # jalankan beneran semua akun (Ctrl+C stop)
"""
import base64
import hashlib
import json
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

try:  # warna opsional — kalau tidak ada colorama, tetap jalan
    from colorama import init as _colorama_init
    _colorama_init()
except Exception:
    pass

HERE = Path(__file__).parent
API = "https://testnet-api.titanx.cc"
ORIGIN = "https://testnet.titanx.cc"
AUD = "Daml-Perp-Request v=2"
ENV_FILE = HERE / ".env"
ACCOUNTS_FILE = HERE / "accounts.json"

SIZE_PCT = 0.75
HOLD_MIN, HOLD_MAX = 60, 240
PAUSE_MIN, PAUSE_MAX = 120, 420

# ---------- warna ----------
def _c(code, s):
    return f"\033[{code}m{s}\033[0m"

def C_R(s):  return _c("1;31", s)   # merah tebal
def C_G(s):  return _c("1;32", s)   # hijau tebal
def C_Y(s):  return _c("1;33", s)   # kuning tebal
def C_B(s):  return _c("1;36", s)   # cyan tebal
def C_M(s):  return _c("1;35", s)   # magenta tebal
def C_DIM(s): return _c("2", s)     # redup

# satu lock supaya print dari beberapa akun tidak nyambung
PRINT_LOCK = threading.Lock()

# palet warna per akun (bergilir)
ACC_COLORS = [C_G, C_B, C_M, C_Y, C_R, (lambda s: _c("1;34", s)),
              (lambda s: _c("1;95", s)), (lambda s: _c("1;96", s))]


def log(acct, msg, color=None):
    """Log dengan nama akun berwarna; aman dipakai dari thread mana pun."""
    color = color or acct.get("color")
    tag = color(f"[{acct['name']}]")
    with PRINT_LOCK:
        print(f"[{datetime.now():%H:%M:%S}] {tag} {msg}", flush=True)


def log_global(msg, color=C_B):
    with PRINT_LOCK:
        print(f"[{datetime.now():%H:%M:%S}] {color(msg)}", flush=True)


# ---------- env / akun ----------
def load_env():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def load_accounts():
    """Kembalikan daftar akun: dari accounts.json, atau fallback .env."""
    accs = []
    if ACCOUNTS_FILE.exists():
        try:
            data = json.loads(ACCOUNTS_FILE.read_text())
            for i, a in enumerate(data):
                if a.get("token"):
                    accs.append({
                        "name": a.get("name") or f"akun{i + 1}",
                        "token": a["token"].strip(),
                        "size_pct": float(a.get("size_pct", SIZE_PCT)),
                    })
        except Exception as e:
            print(f"accounts.json rusak ({e}) — diabaikan")
    if not accs:
        # fallback: TITANX_TOKEN, lalu TITANX_TOKEN_1..N
        tok = os.environ.get("TITANX_TOKEN", "").strip()
        if tok:
            accs.append({"name": "default", "token": tok, "size_pct": SIZE_PCT})
        i = 1
        while True:
            tok = os.environ.get(f"TITANX_TOKEN_{i}", "").strip()
            if not tok:
                break
            accs.append({"name": f"akun{i}", "token": tok, "size_pct": SIZE_PCT})
            i += 1
    for i, a in enumerate(accs):
        a["color"] = ACC_COLORS[i % len(ACC_COLORS)]
        a["env_file"] = HERE / f".env_{a['name']}"          # token per akun
        a["key_file"] = HERE / f"session_key_{a['name']}.bin"
        a["session_file"] = HERE / f"session_{a['name']}.json"
    return accs


def get_token(acct) -> str:
    tok = acct["token"]
    if not tok:
        sys.exit(f"token akun {acct['name']} kosong")
    return tok


def save_env_key(acct, key, value):
    f = acct["env_file"]
    lines = []
    if f.exists():
        lines = [l for l in f.read_text().splitlines() if not l.startswith(key + "=")]
    lines.append(f"{key}={value}")
    f.write_text("\n".join(lines) + "\n")


# ---------- key & session (per akun) ----------
def load_or_create_key(acct) -> Ed25519PrivateKey:
    kf = acct["key_file"]
    if kf.exists():
        raw = kf.read_bytes()
    else:
        priv = Ed25519PrivateKey.generate()
        raw = priv.private_bytes(serialization.Encoding.Raw,
                                 serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption())
        kf.write_bytes(raw)
    return Ed25519PrivateKey.from_private_bytes(raw)


def priv_to_hex(priv: Ed25519PrivateKey) -> str:
    return priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def register_session(token: str, pub_hex: str) -> dict:
    """POST /v1/auth/session — daftarkan keypair baru, dapat sessionId. (tidak diubah)"""
    expiry = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    body = {
        "scheme": "jwt",
        "token": token,
        "sessionPubKey": pub_hex,
        "expiry": expiry,
        "maxNotional": "1000000.0000000000",
    }
    h = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": ORIGIN,
        "Referer": ORIGIN + "/",
        "User-Agent": os.environ.get("TITANX_UA", "Mozilla/5.0 (Linux; Android 16; Pixel 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Mobile Safari/537.36"),
        "accept-language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }
    cookies = os.environ.get("TITANX_COOKIES", "").strip()
    if cookies:
        h["Cookie"] = cookies
    r = requests.post(f"{API}/v1/auth/session", json=body, headers=h, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"register session gagal {r.status_code}: {r.text[:300]}")
    return r.json()


def register_via_browser(token, pub_hex):
    """Buka browser (CDP), jalankan fetch POST /v1/auth/session dari halaman titanx. (tidak diubah)"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("pip install playwright  (sekali saja, hanya untuk register awal)")
    cdp = os.environ.get("CDP_URL", "http://127.0.0.1:9222")
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp)
        ctx = browser.contexts[0]
        page = next((pg for pg in ctx.pages if "titanx.cc" in pg.url), None)
        if page is None:
            page = ctx.new_page()
            page.goto(ORIGIN + "/trade/btc-usdcx", wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
        expiry = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        js = """
        async ({token, pub, expiry}) => {
          const r = await fetch('%s/v1/auth/session', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            credentials: 'include',
            body: JSON.stringify({scheme:'jwt', token, sessionPubKey: pub, expiry, maxNotional: '1000000.0000000000'})
          });
          const txt = await r.text();
          return {status: r.status, body: txt};
        }
        """ % API
        res = page.evaluate(js, {"token": token, "pub": pub_hex, "expiry": expiry})
        return res


def get_session(acct):
    """Pakai sessionId tersimpan per akun; kalau tidak ada/expired, daftarkan baru."""
    priv = load_or_create_key(acct)
    pub_hex = priv_to_hex(priv)
    sf = acct["session_file"]
    if sf.exists():
        try:
            s = json.loads(sf.read_text())
            if s.get("pub") == pub_hex and s.get("sessionId"):
                return priv, s["sessionId"]
        except Exception:
            pass
    log(acct, "mendaftarkan session key baru...")
    token = get_token(acct)
    try:
        s = register_session(token, pub_hex)
    except RuntimeError as e:
        log(acct, "register langsung gagal (403 Cloudflare) -> coba lewat browser (CDP)...", C_Y)
        if "403" in str(e):
            res = register_via_browser(token, pub_hex)
            log(acct, f"browser register: {res['status']} {res['body'][:200]}")
            if res["status"] != 200:
                sys.exit("register via browser juga gagal. Pastikan start-browser.bat jalan & sudah login di titanx.")
            s = json.loads(res["body"])
        else:
            raise
    sf.write_text(json.dumps({"sessionId": s.get("sessionId") or s.get("id"),
                              "pub": pub_hex, "raw": s}))
    log(acct, f"session OK ({sf.name})")
    return priv, s.get("sessionId") or s.get("id")


# ---------- signing (TIDAK DIUBAH) ----------
def H(k, v):
    return f"{k}: {v}"  # NOTE: request message uses single-space format (hn), NOT padded


def fmt_ts(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, timezone.utc)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    frac = f"{ms % 1000:03d}".rstrip("0")
    return base + (f".{frac}Z" if frac else "Z")


def query_canon(qs: str) -> str:
    """Replikasi fn() di JS: sort query params by encoded key."""
    if qs.startswith("?"):
        qs = qs[1:]
    if not qs:
        return ""
    parts = [p for p in qs.split("&") if p]
    def key(p):
        k = p.split("=", 1)[0]
        return k.encode()
    return "&".join(sorted(parts, key=key))


def build_message(session_id, method, path, query="", body=None) -> str:
    nonce = int(time.time() * 1000)
    body_sha = hashlib.sha256((body or "").encode()).hexdigest()
    lines = [
        AUD,
        H("aud", "rest"),
        H("origin", API),
        H("session", session_id),
        H("conn", ""),
        H("method", method.upper()),
        H("path", path),
        H("query", query_canon(query)),
        H("body", body_sha),
        H("ts", fmt_ts(nonce)),
        H("nonce", nonce),
        H("expires", ""),
    ]
    return "\n".join(lines), nonce


def headers_for(priv, session_id, method, path, query="", body=None, body_str=None):
    body_for_sig = body_str if body_str is not None else (json.dumps(body) if body is not None else None)
    msg, nonce = build_message(session_id, method, path, query, body_for_sig)
    sig = priv.sign(msg.encode()).hex()
    return {
        "X-Session-Id": session_id,
        "X-Request-Ts": fmt_ts(nonce),
        "X-Request-Nonce": str(nonce),
        "X-Request-Sig": sig,
        "Authorization": f"Bearer {session_id and priv and _current_token()}",  # diisi via token acct (lihat api())
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": ORIGIN,
        "Referer": ORIGIN + "/",
        "User-Agent": os.environ.get("TITANX_UA", "Mozilla/5.0 (Linux; Android 16; Pixel 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Mobile Safari/537.36"),
        "accept-language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "priority": "u=1, i",
    }


def api(acct, priv, sid, method, path, query="", body=None):
    """Sama seperti v2, tapi Authorization memakai token akun terkait."""
    url = API + path + (f"?{query}" if query else "")
    data = json.dumps(body) if body is not None else None
    h = headers_for(priv, sid, method, path, query, body, body_str=data)
    h["Authorization"] = f"Bearer {acct['token']}"
    cookies = os.environ.get("TITANX_COOKIES", "").strip()
    if cookies:
        h["Cookie"] = cookies
    r = requests.request(method, url, headers=h, data=data, timeout=30)
    return r


# ---------- trading (logika TIDAK DIUBAH, parameterized per akun) ----------
def get_markets(acct, priv, sid):
    r = api(acct, priv, sid, "GET", "/v1/markets")
    if r.status_code != 200:
        return []
    d = r.json()
    return d.get("markets", d if isinstance(d, list) else [])


def get_balance(acct, priv, sid):
    r = api(acct, priv, sid, "GET", "/v1/me/balance")
    if r.status_code != 200:
        log(acct, f"balance {r.status_code}: {r.text[:150]}", C_R)
        return None
    return float(r.json().get("freeBalance") or r.json().get("balance") or 0)


def get_positions(acct, priv, sid):
    r = api(acct, priv, sid, "GET", "/v1/me/positions")
    if r.status_code != 200:
        return []
    d = r.json()
    return d.get("items", d if isinstance(d, list) else [])


def get_mark_price(acct, priv, sid, market):
    r = api(acct, priv, sid, "GET", f"/v1/markets/{market}/mark")
    if r.status_code == 200:
        try:
            d = r.json()
            if isinstance(d, dict):
                for k in ("markPrice", "price", "mid", "last"):
                    if d.get(k):
                        return float(d[k])
                if d.get("mark") and isinstance(d["mark"], dict):
                    for k in ("price", "markPrice", "mid"):
                        if d["mark"].get(k):
                            return float(d["mark"][k])
        except Exception:
            pass
    log(acct, f"gagal ambil harga mark {market}: {r.status_code} {r.text[:120]}", C_Y)
    return None


def canon_dec(x: str) -> str:
    """Replikasi U() di JS: decimal -> X.XXXXXXXXXX (10 digit pecahan). (tidak diubah)"""
    x = str(x).strip()
    neg, rest = ("-", x[1:]) if x.startswith("-") else ("", x)
    if "." in rest:
        i, f = rest.split(".", 1)
    else:
        i, f = rest, ""
    f = (f + "0" * 10)[:10]
    i = i.lstrip("0") or "0"
    if i == "0" and set(f) <= {"0"}:
        return "0.0000000000"
    return f"{neg}{i}.{f}"


def Hp(k, v):
    return f"{k}:".ljust(14) + str(v)


def order_headers(priv, session_id, market, order, nonce, ts_str):
    """Header order: X-Session-Sig atas pesan 'Daml-Perp-Order v=1'. (tidak diubah)"""
    lines = [
        "Daml-Perp-Order v=1",
        Hp("sessionId", session_id),
        Hp("market", market),
        Hp("side", order["side"]),
        Hp("type", order["type"]),
        Hp("size", canon_dec(order["size"])),
        Hp("price", canon_dec(order.get("price", "0"))),
        Hp("tif", order.get("tif", "") or ""),
        Hp("reduceOnly", "true" if order.get("reduceOnly") is True else "false"),
        Hp("tpTrigger", canon_dec("0")),
        Hp("tpLimit", canon_dec("0")),
        Hp("tpSize", canon_dec("0")),
        Hp("slTrigger", canon_dec("0")),
        Hp("slLimit", canon_dec("0")),
        Hp("slSize", canon_dec("0")),
        Hp("clientOrderId", ""),
        Hp("nonce", nonce),
        Hp("ts", ts_str),
    ]
    msg = "\n".join(lines)
    return {
        "X-Session-Id": session_id,
        "X-Session-Sig": priv.sign(msg.encode()).hex(),
    }


def place_order(acct, priv, sid, market, side, size_str, mkt_meta, reduce_only=False):
    nonce = int(time.time() * 1000)
    ts_str = fmt_ts(nonce)
    order = {"side": side, "type": "market", "size": size_str,
             "price": "0", "tif": "ioc", "reduceOnly": reduce_only}
    body = {
        "market": market,
        **{k: v for k, v in order.items() if not (k == "tif" and v == "")},
        "nonce": nonce,
        "ts": ts_str,
    }
    h = headers_for(priv, sid, "POST", "/v1/orders", body=body, body_str=json.dumps(body))
    h["Authorization"] = f"Bearer {acct['token']}"
    h.update(order_headers(priv, sid, market, order, nonce, ts_str))
    data = json.dumps(body)
    cookies = os.environ.get("TITANX_COOKIES", "").strip()
    if cookies:
        h["Cookie"] = cookies
    r = requests.post(f"{API}/v1/orders", headers=h, data=data, timeout=30)
    ok = r.status_code in (200, 201)
    if ok:
        fee = ""
        try:
            j = r.json()
            if isinstance(j, dict) and j.get("fee"):
                fv = float(j["fee"])
                fee = "  " + C_DIM("fee %.4f" % fv)
        except Exception:
            pass
        log(acct, f"{C_G('✓ ORDER ' + side.upper())} {market} {C_DIM(f'size={size_str} → 200')}{fee}")
    else:
        log(acct, f"{C_R('✗ ORDER ' + side.upper())} {market} {C_DIM(f'size={size_str} → {r.status_code}')} {C_R(r.text[:120])}")
    return ok


def round_size(x: float, dec: int) -> str:
    q = round(x, dec)
    if q <= 0:
        q = 10 ** -dec
    return f"{q:.{dec}f}"


MARKET_CACHE = {}
MARKET_LOCK = threading.Lock()


def close_all(acct, priv, sid):
    closed = 0
    with MARKET_LOCK:
        cache = MARKET_CACHE.get(acct["name"], [])
    for p in get_positions(acct, priv, sid):
        market = p.get("market")
        size = abs(float(p.get("size", 0)))
        is_long = float(p.get("size", 0)) > 0
        side = "sell" if is_long else "buy"
        meta = next((m for m in cache if m.get("market") == market), {})
        dec = int(meta.get("szDecimals", 4))
        ok = place_order(acct, priv, sid, market, side, round_size(size, dec), meta, reduce_only=True)
        if ok:
            closed += 1
        time.sleep(1.5)
    return closed


STOP_EVENT = threading.Event()


def pause_bar(acct, secs):
    """Jeda dengan progress bar berjalan; langsung selesai kalau Ctrl+C."""
    width = 24
    end = time.time() + secs
    while time.time() < end and not STOP_EVENT.is_set():
        left = int(end - time.time())
        done = int(width * (secs - left) / max(secs, 1))
        bar = "█" * done + "░" * (width - done)
        with PRINT_LOCK:
            sys.stdout.write(f"\r  {acct['color']('[' + acct['name'] + ']')} jeda {bar} {left:3d}s  ")
            sys.stdout.flush()
        time.sleep(1)
    with PRINT_LOCK:
        sys.stdout.write("\r" + " " * 70 + "\r")
        sys.stdout.flush()


ALL_MARKETS = True           # open posisi di SEMUA market setiap siklus
SIZE_PCT_MIN, SIZE_PCT_MAX = 0.50, 0.75   # notional per posisi = 50%-75% (dibagi jumlah posisi)


def random_size_pct():
    return random.uniform(SIZE_PCT_MIN, SIZE_PCT_MAX)


def cycle(acct, priv, sid, dry=False):
    """Satu siklus: open posisi di SEMUA market (size 50-75% random), tahan, tutup semua."""
    markets = get_markets(acct, priv, sid)
    if not markets:
        log(acct, "tidak bisa ambil markets")
        return False
    with MARKET_LOCK:
        MARKET_CACHE[acct["name"]] = markets

    bal = get_balance(acct, priv, sid)
    if bal is None:
        return False

    # ambil harga semua market dulu
    prices = {}
    for m in markets:
        market = m.get("market")
        price = get_mark_price(acct, priv, sid, market)
        if price and price > 0:
            prices[market] = price
        time.sleep(0.2)
    if not prices:
        log(acct, "tidak ada harga yang terbaca — siklus dilewati", C_Y)
        return False

    # alokasi: total 50-75% saldo dibagi rata ke semua market yang harganya terbaca
    total_pct = random_size_pct()
    n = len(prices)
    per_pos = bal * total_pct / n
    log(acct, f"saldo ${bal:.2f} | open {n} market | total notional=${bal * total_pct:.2f} "
              f"({total_pct * 100:.0f}%) ≈ ${per_pos:.2f}/market", C_M)

    opened = 0
    for m in markets:
        market = m.get("market")
        if market not in prices:
            continue
        price = prices[market]
        dec = int(m.get("szDecimals", 4))
        min_notional = float(m.get("minNotional", 10))
        notional = max(per_pos, min_notional)
        size = notional / price
        size_str = round_size(size, dec)
        side = random.choice(["buy", "sell"])
        pos_side = "LONG" if side == "buy" else "SHORT"
        log(acct, f"→ {market} {pos_side} notional=${notional:.2f} size={size_str} (harga {price})")
        if not dry:
            place_order(acct, priv, sid, market, side, size_str, m)
            time.sleep(0.5)
        else:
            log(acct, "[DRY] tidak kirim order", C_Y)
        opened += 1

    if dry or opened == 0:
        return opened > 0

    hold = random.randint(HOLD_MIN, HOLD_MAX)
    log(acct, f"menahan {opened} posisi {hold}s ...", C_DIM)
    for _ in range(hold):
        if STOP_EVENT.is_set():
            break
        time.sleep(1)
    n = close_all(acct, priv, sid)
    log(acct, f"posisi ditutup: {n}")
    return True


# ---------- thread per akun ----------
def run_account(acct, dry=False):
    try:
        priv, sid = get_session(acct)
        if not dry:
            try:
                n = close_all(acct, priv, sid)
                if n:
                    log(acct, f"posisi lama ditutup saat mulai: {n}")
            except Exception as e:
                log(acct, f"gagal cek/tutup posisi lama: {e}", C_Y)
        while not STOP_EVENT.is_set():
            try:
                cycle(acct, priv, sid, dry=dry)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log(acct, f"error: {e}", C_R)
                if "401" in str(e) or "unauthorized" in str(e).lower():
                    acct["session_file"].unlink(missing_ok=True)
                    priv, sid = get_session(acct)
            if STOP_EVENT.is_set():
                break
            nap = random.randint(PAUSE_MIN, PAUSE_MAX)
            log(acct, f"jeda {nap}s", C_DIM)
            pause_bar(acct, nap)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log(acct, f"thread mati: {e}", C_R)


def banner(accts, mode):
    W = 56
    def row(s=""):
        return C_B("║") + s + " " * max(0, W - _plen(s)) + C_B("║")
    print()
    print(C_B("╔" + "═" * W + "╗"))
    print(row(C_M("  ⚡ TITANX BOT v3 — SASA")))
    print(row(f"  mode: {mode}    akun aktif: {len(accts)}    market: ALL"))
    print(row("  " + "  ·  ".join(a["color"]("● " + a["name"]) for a in accts)))
    print(C_B("╚" + "═" * W + "╝"))
    print()


def _plen(s):
    """panjang string polos (tanpa kode warna ANSI)."""
    import re
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


def _current_token():
    return os.environ.get("TITANX_TOKEN", "")


# ---------- commands ----------
def cmd_status():
    """Tabel status semua akun: saldo, posisi terbuka, PnL bila ada."""
    load_env()
    accts = load_accounts()
    if not accts:
        sys.exit("tidak ada akun (isi accounts.json atau TITANX_TOKEN di .env)")
    rows = []
    for a in accts:
        try:
            priv, sid = get_session(a)
            bal = get_balance(a, priv, sid)
            pos = get_positions(a, priv, sid)
            rows.append((a, bal, pos))
        except Exception as e:
            rows.append((a, None, [{"err": str(e)}]))
    print()
    print(C_B(f"  {'AKUN':<12} {'SALDO':>12}  {'POSISI TERBUKA'}"))
    print(C_DIM("  " + "─" * 60))
    total = 0.0
    for a, bal, pos in rows:
        if bal is None:
            print(f"  {a['color'](a['name']):<20} {C_R('gagal ambil saldo')}  {pos}")
            continue
        total += bal
        if not pos:
            print(f"  {a['color'](a['name']):<20} {C_G(f'${bal:>10.2f}')}  {C_DIM('—')}")
        for p in pos:
            sz = float(p.get("size", 0))
            side = C_G("LONG") if sz > 0 else C_R("SHORT")
            print(f"  {a['color'](a['name']):<20} {C_G(f'${bal:>10.2f}')}  {side} {p.get('market')} size={abs(sz)}")
    print(C_DIM("  " + "─" * 60))
    print(f"  {C_B('TOTAL'):<20} {C_G(f'${total:.2f}')}")
    print()


def cmd_probe():
    load_env()
    accts = load_accounts()
    if not accts:
        sys.exit("tidak ada akun (isi accounts.json atau TITANX_TOKEN di .env)")
    banner(accts, "probe")
    for a in accts:
        log_global(f"=== PROBE [{a['name']}] ===", a["color"])
        priv, sid = get_session(a)
        r = api(a, priv, sid, "GET", "/v1/markets")
        log(a, f"GET /v1/markets -> {r.status_code}")
        if r.status_code == 200:
            ms = r.json().get("markets", [])
            for m in ms:
                log(a, f"  {m.get('market'):12} szDec={m.get('szDecimals')} minNotional={m.get('minNotional')} maxLev={m.get('maxLeverage')}", C_DIM)
        r = api(a, priv, sid, "GET", "/v1/me/balance")
        log(a, f"GET /v1/me/balance -> {r.status_code} {r.text[:200]}")
        r = api(a, priv, sid, "GET", "/v1/me/positions")
        log(a, f"GET /v1/me/positions -> {r.status_code} {r.text[:200]}")
        print()


def cmd_set_token():
    load_env()
    accts = load_accounts()
    print("Simpan token untuk akun mana?")
    names = [a["name"] for a in accts] + ["(akun baru)"]
    for i, n in enumerate(names):
        print(f"  {i + 1}. {n}")
    try:
        idx = int(input("nomor: ").strip()) - 1
    except Exception:
        return
    tok = input("Paste access token (jwt, eyJ...): ").strip()
    if not tok:
        return
    if 0 <= idx < len(accts):
        acct = accts[idx]
        save_env_key(acct, "TITANX_TOKEN", tok)
        acct["session_file"].unlink(missing_ok=True)
        print(f"token akun {acct['name']} disimpan, session di-reset. Jalankan: python titanx_bot3.py probe")
    else:
        # akun baru -> simpan sebagai TITANX_TOKEN_<n> di .env
        n = 1
        while os.environ.get(f"TITANX_TOKEN_{n}"):
            n += 1
        lines = [l for l in (ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else [])]
        lines.append(f"TITANX_TOKEN_{n}={tok}")
        ENV_FILE.write_text("\n".join(lines) + "\n")
        print(f"disimpan sebagai TITANX_TOKEN_{n} di .env. Jalankan: python titanx_bot3.py probe")


def main():
    load_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"

    if cmd == "set-token":
        cmd_set_token()
        return

    accts = load_accounts()
    if not accts:
        sys.exit("tidak ada akun (isi accounts.json atau TITANX_TOKEN di .env)")

    if cmd == "status":
        cmd_status()
    elif cmd == "probe":
        cmd_probe()
    elif cmd == "dry":
        banner(accts, "dry")
        threads = [threading.Thread(target=run_account, args=(a, True), daemon=True) for a in accts]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    elif cmd == "run":
        banner(accts, "run")
        threads = [threading.Thread(target=run_account, args=(a, False), daemon=True) for a in accts]
        for t in threads:
            t.start()
        try:
            while any(t.is_alive() for t in threads):
                time.sleep(0.5)
        except KeyboardInterrupt:
            log_global("stop (Ctrl+C) — menutup posisi semua akun...", C_Y)
            STOP_EVENT.set()
            for t in threads:
                t.join(timeout=15)
            log_global("selesai.", C_B)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
