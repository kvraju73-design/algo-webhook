"""
Flattrade MULTI-INSTRUMENT - CLOUD VERSION v7
FIXES v7:
  - ✅ Switched to LMT orders (Flattrade API rejects MKT — "ALGO_CHK" error)
  - ✅ Aggressive 3% LMT buffer to simulate market fills on volatile contracts
  - ✅ Configurable buffer via LMT_BUFFER_PCT env variable
  - ✅ Split BUY/EXIT debounce keys (exits never blocked by prior buys)
  - ✅ Startup warnings for missing SELF_URL / bad config
  - ✅ Clean logs with LTP + fill price visibility
"""

import os
import json
import hashlib
import logging
import sys
import re
import threading
import time
import requests
from datetime import datetime, time as dtime
from flask import Flask, request, jsonify, render_template_string, redirect
import pytz
from urllib.parse import quote

# =============================================
# ENVIRONMENT CONFIG
# =============================================
USER_ID        = os.environ.get("USER_ID", "")
API_KEY        = os.environ.get("API_KEY", "")
API_SECRET     = os.environ.get("API_SECRET", "")
PROXY_HOST     = os.environ.get("PROXY_HOST", "")
PROXY_PORT     = os.environ.get("PROXY_PORT", "443")
PROXY_USER     = os.environ.get("PROXY_USER", "")
PROXY_PASS     = os.environ.get("PROXY_PASS", "")

# MIS = Intraday (low margin). Auto-squareoff at ~11:20PM MCX
PRODUCT        = os.environ.get("PRODUCT", "I")

DRY_RUN        = os.environ.get("DRY_RUN", "False").lower() == "true"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
SELF_URL       = os.environ.get("SELF_URL", "")

# ⚠️ Flattrade API REJECTS MKT orders ("ALGO_CHK: MKT Order type not allowed")
# Must use LMT with aggressive buffer to simulate market fills
ORDER_TYPE     = os.environ.get("ORDER_TYPE", "LMT").upper()
LMT_BUFFER_PCT = float(os.environ.get("LMT_BUFFER_PCT", "3.0"))

PROXIES = {
    "http":  f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}",
    "https": f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}",
} if PROXY_HOST else None

TOKEN_URL      = "https://authapi.flattrade.in/trade/apitoken"
BASE_URL_ORDER = "https://piconnect.flattrade.in/PiConnectAPI"

# =============================================
# INSTRUMENT CONFIGURATION
# =============================================
INSTRUMENTS = {
    "NIFTY": {
        "exchange":     "NFO",
        "lot_size":     65,
        "market_open":  dtime(9, 15),
        "market_close": dtime(15, 30),
    },
    "BANKNIFTY": {
        "exchange":     "NFO",
        "lot_size":     30,
        "market_open":  dtime(9, 15),
        "market_close": dtime(15, 30),
    },
    "CRUDEOIL": {
        "exchange":     "MCX",
        "lot_size":     100,
        "market_open":  dtime(9, 0),
        "market_close": dtime(23, 30),
    },
}
DEFAULT_LOT_SIZE = 65

# =============================================
# LOGGING
# =============================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# =============================================
# STATE
# =============================================
state = {
    "token":       None,
    "call_symbol": "",
    "put_symbol":  "",
    "instrument":  "NIFTY",
    "qty":         DEFAULT_LOT_SIZE,
    "logs":        [],
    "last_action": {},
    "manual": {
        "NIFTY":     {"call": "", "put": "", "qty": 65,  "lots": 1},
        "BANKNIFTY": {"call": "", "put": "", "qty": 30,  "lots": 1},
        "CRUDEOIL":  {"call": "", "put": "", "qty": 100, "lots": 1},
    },
    "form": {
        "NIFTY":     {"day": "", "mon": "", "yr": "",
                      "ce_strike": "", "pe_strike": "", "lots": "1"},
        "BANKNIFTY": {"day": "", "mon": "", "yr": "",
                      "ce_strike": "", "pe_strike": "", "lots": "1"},
        "CRUDEOIL":  {"day": "", "mon": "", "yr": "",
                      "ce_strike": "", "pe_strike": "", "lots": "1"},
    }
}

IST = pytz.timezone("Asia/Kolkata")


def add_log(msg):
    ts    = datetime.now(IST).strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    state["logs"].append(entry)
    state["logs"] = state["logs"][-200:]
    log.info(msg)
    return entry


# =============================================
# STARTUP WARNINGS
# =============================================
def startup_checks():
    warnings = []
    if not SELF_URL:
        warnings.append("⚠️  CRITICAL: SELF_URL not set → server may sleep → webhooks WILL be dropped!")
    if not USER_ID or not API_KEY or not API_SECRET:
        warnings.append("⚠️  CRITICAL: USER_ID / API_KEY / API_SECRET missing!")
    if ORDER_TYPE not in ("MKT", "LMT"):
        warnings.append(f"⚠️  Invalid ORDER_TYPE={ORDER_TYPE}, must be MKT or LMT")
    if ORDER_TYPE == "MKT":
        warnings.append("⚠️  ORDER_TYPE=MKT — Flattrade API rejects this! Set ORDER_TYPE=LMT")
    if PRODUCT not in ("I", "M", "C", "B", "H"):
        warnings.append(f"⚠️  Unusual PRODUCT={PRODUCT} (expected I=MIS, M=NRML)")
    if LMT_BUFFER_PCT < 0.5 or LMT_BUFFER_PCT > 10:
        warnings.append(f"⚠️  LMT_BUFFER_PCT={LMT_BUFFER_PCT}% seems unusual (typical 1-5%)")

    for w in warnings:
        log.warning(w)
        add_log(w)

    log.info(f"[STARTUP] Server ready | ORDER_TYPE={ORDER_TYPE} | PRODUCT={PRODUCT} | LMT_BUFFER={LMT_BUFFER_PCT}% | DRY_RUN={DRY_RUN}")
    add_log(f"[STARTUP] Server ready | ORDER_TYPE={ORDER_TYPE} | PRODUCT={PRODUCT} | BUFFER={LMT_BUFFER_PCT}%")


# =============================================
# KEEP-ALIVE THREAD (pings /health every 4 min)
# =============================================
def keep_alive_loop():
    time.sleep(30)
    while True:
        try:
            if SELF_URL:
                url = f"{SELF_URL.rstrip('/')}/health"
                r   = requests.get(url, timeout=10)
                log.info(f"[KEEP-ALIVE] Pinged {url} → {r.status_code}")
            else:
                log.warning("[KEEP-ALIVE] SELF_URL not set — SERVER MAY SLEEP!")
        except Exception as e:
            log.warning(f"[KEEP-ALIVE] Ping failed: {e}")
        time.sleep(4 * 60)

threading.Thread(
    target=keep_alive_loop, daemon=True, name="keep-alive"
).start()
log.info("[KEEP-ALIVE] Background ping thread started.")

startup_checks()


# =============================================
# INSTRUMENT HELPERS
# =============================================
def detect_instrument(symbol):
    if not symbol:
        return None
    symbol = symbol.upper()
    if symbol.startswith("BANKNIFTY"):
        return "BANKNIFTY"
    if symbol.startswith("CRUDEOIL"):
        return "CRUDEOIL"
    if symbol.startswith("NIFTY"):
        return "NIFTY"
    return None


def get_instrument_config(symbol):
    instrument = detect_instrument(symbol)
    if instrument and instrument in INSTRUMENTS:
        return instrument, INSTRUMENTS[instrument]
    return None, None


def is_market_open_for(instrument):
    if instrument not in INSTRUMENTS:
        return False
    cfg = INSTRUMENTS[instrument]
    now = datetime.now(IST).time()
    return cfg["market_open"] <= now <= cfg["market_close"]


# =============================================
# TOKEN / LOGIN
# =============================================
def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def exchange_request_code(request_code):
    hash_value = sha256(API_KEY + request_code + API_SECRET)
    payload = {
        "api_key":      API_KEY,
        "request_code": request_code,
        "api_secret":   hash_value
    }
    try:
        r    = requests.post(TOKEN_URL, json=payload, proxies=PROXIES, timeout=15)
        resp = r.json()
    except Exception as e:
        add_log(f"[LOGIN] Failed: {e}")
        return False, str(e)

    if resp.get("stat") != "Ok":
        err = resp.get("emsg", str(resp))
        add_log(f"[LOGIN] Failed: {err}")
        return False, err

    token = resp.get("token") or resp.get("susertoken")
    if not token:
        return False, "No token returned"

    state["token"] = token
    add_log("[LOGIN] Token generated successfully.")
    return True, "OK"


# =============================================
# API CALLS
# =============================================
def get_token_for_symbol(symbol, exchange):
    try:
        r = requests.post(
            BASE_URL_ORDER + "/SearchScrip",
            data=(
                "jData=" + json.dumps({
                    "uid":   USER_ID,
                    "stext": symbol,
                    "exch":  exchange
                }) + "&jKey=" + state["token"]
            ),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            proxies=PROXIES, timeout=8
        )
        resp = r.json()
        if resp.get("stat") == "Ok":
            for v in resp.get("values", []):
                if v.get("tsym") == symbol:
                    return v.get("token")
            if resp.get("values"):
                return resp["values"][0].get("token")
        add_log(f"[LOOKUP] Failed for {symbol}: {resp.get('emsg')}")
    except Exception as e:
        add_log(f"[LOOKUP] Exception: {e}")
    return None


def get_ltp(symbol, exchange):
    tk = get_token_for_symbol(symbol, exchange)
    if not tk:
        return None
    try:
        r = requests.post(
            BASE_URL_ORDER + "/GetQuotes",
            data=(
                "jData=" + json.dumps({
                    "uid":   USER_ID,
                    "exch":  exchange,
                    "token": tk
                }) + "&jKey=" + state["token"]
            ),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            proxies=PROXIES, timeout=8
        )
        resp = r.json()
        if resp.get("stat") == "Ok":
            ltp = float(resp.get("lp", 0))
            add_log(f"[LTP] {symbol} = {ltp}")
            return ltp
    except Exception as e:
        add_log(f"[LTP] Exception: {e}")
    return None


# =============================================
# PLACE ORDER — LMT with aggressive buffer
# =============================================
def place_order(symbol, trantype, qty=None):
    action = "BUY" if trantype == "B" else "SELL"

    instrument, cfg = get_instrument_config(symbol)
    if not cfg:
        add_log(f"[ORDER] ERROR: Unknown instrument for {symbol}")
        return None

    exchange = cfg["exchange"]
    qty      = qty or cfg["lot_size"]

    add_log(f"[ORDER] {action} → {symbol} | qty={qty} | product={PRODUCT} | LMT buffer={LMT_BUFFER_PCT}% | {instrument} ({exchange})")

    if not state["token"]:
        add_log("[ORDER] ERROR: No token. Visit /login first!")
        return None

    if not DRY_RUN and not is_market_open_for(instrument):
        oh = cfg["market_open"].strftime("%H:%M")
        ch = cfg["market_close"].strftime("%H:%M")
        add_log(f"[ORDER] {instrument} market CLOSED ({oh}-{ch} IST)")
        return None

    # Flattrade API requires LMT — use aggressive buffer for guaranteed fill
    ltp = get_ltp(symbol, exchange)
    if ltp is None:
        add_log("[ORDER] LTP fetch failed. Aborting.")
        return None

    buffer_mult = 1 + (LMT_BUFFER_PCT / 100.0)
    price = round(ltp * buffer_mult, 1) if trantype == "B" else round(ltp / buffer_mult, 1)

    if DRY_RUN:
        add_log(f"[DRY RUN] {action} {qty} {symbol} @ {price} (LTP {ltp}, buffer {LMT_BUFFER_PCT}%)")
        return "DRY_RUN"

    payload = {
        "uid":      USER_ID, "actid": USER_ID,
        "exch":     exchange, "tsym":  symbol,
        "qty":      str(qty), "prc":   str(price),
        "prd":      PRODUCT,  "trantype": trantype,
        "prctyp":   "LMT",    "ret":   "DAY"
    }
    try:
        r    = requests.post(
            BASE_URL_ORDER + "/PlaceOrder",
            data="jData=" + json.dumps(payload) + "&jKey=" + state["token"],
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            proxies=PROXIES, timeout=8
        )
        resp = r.json()
        if resp.get("stat") == "Ok":
            ordno = resp.get("norenordno", "")
            add_log(f"[OK] ORDER PLACED | {action} {qty} {symbol} @ {price} (LTP {ltp}) | No: {ordno}")
            return ordno
        add_log(f"[ERR] ORDER FAILED | {resp.get('emsg')}")
    except Exception as e:
        add_log(f"[ERR] Exception: {e}")
    return None


# =============================================
# EXIT POSITION — LMT with aggressive buffer
# =============================================
def exit_position(symbol):
    add_log(f"[EXIT] Triggered for {symbol}")

    instrument, cfg = get_instrument_config(symbol)
    if not cfg:
        add_log(f"[EXIT] ERROR: Unknown instrument for {symbol}")
        return "ERROR: Unknown instrument"

    exchange = cfg["exchange"]

    if not state["token"]:
        add_log("[EXIT] ERROR: No token.")
        return "ERROR: No token"

    if not DRY_RUN and not is_market_open_for(instrument):
        oh = cfg["market_open"].strftime("%H:%M")
        ch = cfg["market_close"].strftime("%H:%M")
        add_log(f"[EXIT] {instrument} market CLOSED ({oh}-{ch} IST)")
        return f"ERROR: Market closed {oh}-{ch}"

    if DRY_RUN:
        add_log(f"[DRY RUN] Would exit {symbol}")
        return "DRY RUN: Exit simulated"

    try:
        r = requests.post(
            BASE_URL_ORDER + "/PositionBook",
            data=(
                "jData=" + json.dumps({"uid": USER_ID, "actid": USER_ID})
                + "&jKey=" + state["token"]
            ),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            proxies=PROXIES, timeout=10
        )
        resp_data = r.json()

        if isinstance(resp_data, dict):
            add_log(f"[EXIT] PositionBook error: {resp_data.get('emsg', resp_data)}")
            return f"ERROR: {resp_data.get('emsg', 'No positions')}"

        if not isinstance(resp_data, list) or len(resp_data) == 0:
            add_log("[EXIT] No open positions found.")
            return "ERROR: No positions"

        def symbols_match(pos_sym, target_sym):
            if pos_sym == target_sym:
                return True
            pos_clean    = re.sub(r'[^A-Z0-9]', '', pos_sym.upper())
            target_clean = re.sub(r'[^A-Z0-9]', '', target_sym.upper())
            if pos_clean == target_clean:
                return True
            m1 = re.search(r'^([A-Z]+)\d+[A-Z]+\d+(C|P)(\d+)$', pos_sym.upper())
            m2 = re.search(r'^([A-Z]+)\d+[A-Z]+\d+(C|P)(\d+)$', target_sym.upper())
            if m1 and m2:
                return (m1.group(1) == m2.group(1) and
                        m1.group(2) == m2.group(2) and
                        m1.group(3) == m2.group(3))
            return False

        found  = False
        result = ""

        for pos in resp_data:
            pos_sym = pos.get("tsym", "")
            if not symbols_match(pos_sym, symbol):
                continue

            found   = True
            net_qty = int(pos.get("netqty", 0))

            if net_qty == 0:
                add_log(f"[EXIT] No open qty in {symbol} (netqty=0)")
                return "No open position to exit"

            trantype = "S" if net_qty > 0 else "B"
            exit_qty = abs(net_qty)
            action   = "SELL" if trantype == "S" else "BUY"

            add_log(f"[EXIT] Found {pos_sym} netqty={net_qty} → {action} {exit_qty} @ LMT buffer={LMT_BUFFER_PCT}%")

            ltp = get_ltp(pos_sym, exchange)
            if ltp is None:
                ltp = float(pos.get("lp", 0)) or float(pos.get("upldprc", 100))
                add_log(f"[EXIT] Using fallback LTP: {ltp}")

            buffer_mult = 1 + (LMT_BUFFER_PCT / 100.0)
            # SELL below market, BUY above market → guaranteed fill
            price = round(ltp / buffer_mult, 1) if trantype == "S" else round(ltp * buffer_mult, 1)

            sq_payload = {
                "uid":      USER_ID, "actid":    USER_ID,
                "exch":     exchange, "tsym":     pos_sym,
                "qty":      str(exit_qty), "prc": str(price),
                "prd":      PRODUCT,  "trantype": trantype,
                "prctyp":   "LMT",    "ret":      "DAY"
            }
            sq_r    = requests.post(
                BASE_URL_ORDER + "/PlaceOrder",
                data="jData=" + json.dumps(sq_payload) + "&jKey=" + state["token"],
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                proxies=PROXIES, timeout=8
            )
            sq_resp = sq_r.json()

            if sq_resp.get("stat") == "Ok":
                ordno  = sq_resp.get("norenordno", "")
                result = f"EXITED {pos_sym} qty={exit_qty} @ {price} (LTP {ltp}) | Order#{ordno}"
                add_log(f"[OK] {result}")
            else:
                err    = sq_resp.get("emsg", "Unknown error")
                result = f"EXIT FAILED: {err}"
                add_log(f"[ERR] {result}")

        if not found:
            available = [p.get("tsym", "") for p in resp_data if int(p.get("netqty", 0)) != 0]
            msg = f"[EXIT] {symbol} not found. Open positions: {available}"
            add_log(msg)
            return f"Not in positions. Open: {available}"

        return result

    except Exception as e:
        add_log(f"[EXIT] Exception: {e}")
        return f"ERROR: {e}"


# =============================================
# WEBHOOK ACTION HANDLER (split BUY/EXIT debounce)
# =============================================
def handle_action(action, qty=None, symbol=None):
    action = action.upper().strip()

    is_exit_action = action in ("SELL", "EXIT", "EXIT_CALL", "EXIT_PUT", "EXIT_ALL")
    action_class   = "EXIT" if is_exit_action else "ENTRY"
    dedup_key      = f"{action_class}_{symbol or ''}"

    now = time.time()
    if now - state["last_action"].get(dedup_key, 0) < 1:
        add_log(f"[DEBOUNCE] Blocked duplicate: {action} {symbol or ''} (<1s)")
        return
    state["last_action"][dedup_key] = now

    if symbol:
        symbol     = symbol.strip().upper()
        instrument = detect_instrument(symbol)

        if not qty and instrument in INSTRUMENTS:
            qty = INSTRUMENTS[instrument]["lot_size"]
        elif not qty:
            qty = DEFAULT_LOT_SIZE

        add_log(f"[ACTION] {action} | {symbol} | Instrument={instrument} | Qty={qty}")

        if action in ("BUY", "BUY_CALL", "BUY_PUT"):
            place_order(symbol, "B", qty)
        elif is_exit_action:
            exit_position(symbol)
        elif action in ("SELL_SHORT", "SHORT"):
            place_order(symbol, "S", qty)
        else:
            add_log(f"[ACTION] Unknown action: {action}")
        return

    ce  = state["call_symbol"]
    pe  = state["put_symbol"]
    qty = qty or state["qty"]

    if not ce or not pe:
        add_log("[ACTION] ERROR: No symbol in alert AND strikes not set at /setup!")
        return

    action_map = {
        "BUY_CALL":  lambda: place_order(ce, "B", qty),
        "BUY_PUT":   lambda: place_order(pe, "B", qty),
        "EXIT_CALL": lambda: exit_position(ce),
        "EXIT_PUT":  lambda: exit_position(pe),
        "EXIT_ALL":  lambda: [exit_position(ce), exit_position(pe)],
        "SELL_CALL": lambda: place_order(ce, "S", qty),
        "SELL_PUT":  lambda: place_order(pe, "S", qty),
    }

    fn = action_map.get(action)
    if fn:
        fn()
    else:
        add_log(f"[ACTION] Unknown: {action}")


# =============================================
# FLASK APP
# =============================================
app = Flask(__name__)

# =============================================
# HTML TEMPLATES
# =============================================
HOME_HTML = """
<!DOCTYPE html><html><head><title>Flattrade Multi-Instrument v7</title>
<style>
body{font-family:Arial;background:#1a1a2e;color:#eee;padding:30px;
     max-width:900px;margin:auto;}
h1{color:#00d9ff;}h2{color:#ffd700;margin-top:30px;}
.status{padding:15px;background:#16213e;border-radius:8px;margin:10px 0;}
.ok{color:#4caf50;}.err{color:#f44336;}.warn{color:#ff9800;}
a{color:#00d9ff;text-decoration:none;padding:10px 20px;background:#0f3460;
  border-radius:5px;display:inline-block;margin:5px;}
a:hover{background:#e94560;}
table{width:100%;border-collapse:collapse;background:#16213e;
      border-radius:8px;overflow:hidden;}
th,td{padding:10px;text-align:left;border-bottom:1px solid #0f3460;}
th{background:#0f3460;color:#ffd700;}
code{background:#0a0e27;padding:3px 6px;border-radius:3px;color:#00d9ff;}
</style></head><body>
<h1>🚀 Flattrade Algo — Multi-Instrument v7</h1>
<div class="status">
<b>Token:</b>
<span class="{{ 'ok' if token else 'err' }}">
  {{ '✅ Active' if token else '❌ Not logged in' }}
</span><br>
<b>Mode:</b>
<span class="{{ 'warn' if dry else 'ok' }}">
  {{ 'DRY RUN' if dry else 'LIVE' }}
</span><br>
<b>Product:</b>
<span class="{{ 'ok' if product == 'I' else 'warn' }}">
  {{ 'MIS (Intraday) ✅' if product == 'I' else 'NRML ⚠️ High margin needed' }}
</span><br>
<b>Order Type:</b>
<span class="{{ 'ok' if order_type == 'LMT' else 'err' }}">
  {{ 'LMT @ ' + buffer|string + '% buffer ✅' if order_type == 'LMT' else 'MKT ❌ Flattrade will reject!' }}
</span><br>
<b>Keep-Alive:</b>
<span class="{{ 'ok' if self_url else 'err' }}">
  {{ '✅ Active → ' + self_url if self_url else '❌ SELF_URL NOT SET — SERVER WILL SLEEP!' }}
</span><br>
<b>Server Time (IST):</b> {{ time_now }}
</div>

{% if not self_url %}
<div style="background:#7b1c1c;padding:12px;border-radius:8px;margin:10px 0;">
  🚨 <b>CRITICAL:</b> SELF_URL not set! Server will sleep → webhooks WILL be dropped.
  <br>Set env var: <code>SELF_URL=https://your-app.up.railway.app</code>
</div>
{% endif %}

{% if product != 'I' %}
<div style="background:#7b1c1c;padding:12px;border-radius:8px;margin:10px 0;">
  ⚠️ <b>WARNING:</b> PRODUCT=NRML requires ~₹1.8L margin/lot.
  Set <code>PRODUCT=I</code> for MIS (low margin).
</div>
{% endif %}

{% if order_type != 'LMT' %}
<div style="background:#7b1c1c;padding:12px;border-radius:8px;margin:10px 0;">
  🚨 <b>CRITICAL:</b> Flattrade API rejects MKT orders! Set <code>ORDER_TYPE=LMT</code>
</div>
{% endif %}

<h2>📊 Supported Instruments</h2>
<table>
<tr><th>Instrument</th><th>Exchange</th><th>Lot Size</th>
    <th>Market Hours (IST)</th><th>Status</th></tr>
{% for name, cfg in instruments.items() %}
<tr>
<td><b>{{ name }}</b></td><td>{{ cfg.exchange }}</td>
<td>{{ cfg.lot_size }}</td>
<td>{{ cfg.market_open.strftime('%H:%M') }} - {{ cfg.market_close.strftime('%H:%M') }}</td>
<td>{% if market_status[name] %}
<span class="ok">🟢 OPEN</span>
{% else %}<span class="warn">🔴 CLOSED</span>{% endif %}</td>
</tr>{% endfor %}
</table>
<h2>Quick Actions</h2>
<a href="/login">🔑 Daily Login</a>
<a href="/manual">⚡ Manual Trading</a>
<a href="/logs">📋 View Logs</a>
<a href="/health">💚 Health</a>
</body></html>
"""

LOGIN_HTML = """
<!DOCTYPE html><html><head><title>Login</title>
<style>
body{font-family:Arial;background:#1a1a2e;color:#eee;
     padding:30px;max-width:700px;margin:auto;}
h1{color:#00d9ff;}
a.btn,button{background:#e94560;color:white;padding:12px 25px;
  border:none;border-radius:5px;text-decoration:none;
  font-size:16px;cursor:pointer;display:inline-block;}
input{width:100%;padding:10px;font-size:16px;background:#0f3460;
  color:white;border:1px solid #00d9ff;border-radius:5px;
  margin:10px 0;box-sizing:border-box;}
.step{margin:20px 0;padding:15px;background:#0f3460;
      border-left:4px solid #00d9ff;}
.msg{padding:15px;border-radius:5px;margin:15px 0;}
.ok{background:#1b5e20;}.err{background:#b71c1c;}
</style></head><body>
<h1>🔑 Daily Flattrade Login</h1>
{% if msg %}
<div class="msg {{ 'ok' if success else 'err' }}">{{ msg }}</div>
{% endif %}
<div class="step"><b>Step 1:</b><br><br>
<a class="btn" href="https://auth.flattrade.in/?app_key={{ api_key }}" target="_blank">
  🚀 Login to Flattrade
</a></div>
<div class="step"><b>Step 2:</b> Paste request_code from redirect URL:</div>
<form method="POST">
<input name="request_code" placeholder="Paste request_code" required autofocus>
<button type="submit">Generate Token</button>
</form>
<br><a href="/" style="color:#00d9ff">← Back</a>
</body></html>
"""

MANUAL_HTML = """
<!DOCTYPE html>
<html>
<head>
<title>Manual Trading — {{ inst }}</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: Arial, sans-serif; background: #1a1a2e;
       color: #eee; padding: 12px; max-width: 480px; margin: auto; }
h1  { color: #00d9ff; font-size: 20px; margin-bottom: 10px; text-align: center; }
h2  { color: #ffd700; font-size: 15px; margin: 14px 0 6px; }
.warn-box { background: #7b1c1c; border-radius: 8px;
            padding: 10px 14px; margin-bottom: 12px;
            font-size: 13px; text-align: center; }
.ok-box   { background: #1b5e20; border-radius: 8px;
            padding: 8px 14px; margin-bottom: 12px;
            font-size: 13px; text-align: center; }
.tabs { display: flex; gap: 6px; margin-bottom: 14px; }
.tab  { flex: 1; padding: 10px 4px; text-align: center;
        border-radius: 8px; background: #0f3460; color: #aaa;
        font-size: 13px; font-weight: bold; cursor: pointer;
        border: 2px solid transparent; text-decoration: none; }
.tab.active { background: #e94560; color: white; border-color: #ff6b6b; }
.card  { background: #16213e; border-radius: 10px; padding: 14px; margin-bottom: 14px; }
label  { display: block; color: #ffd700; font-size: 12px; margin: 8px 0 3px; }
input, select { width: 100%; padding: 9px 10px; font-size: 15px;
                background: #0f3460; color: white;
                border: 1px solid #00d9ff; border-radius: 6px; }
input:focus { border-color: #ffd700; outline: none; background: #1a2a4a; }
.row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.row3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
.save-btn { width: 100%; margin-top: 12px; padding: 13px;
            font-size: 16px; background: #00d9ff; color: #000;
            border: none; border-radius: 8px; font-weight: bold; cursor: pointer; }
.save-btn:active { background: #0099bb; }
.sym-box { background: #0a0e27; border-radius: 8px;
           padding: 10px 12px; margin-bottom: 12px;
           font-size: 13px; line-height: 2; border: 1px solid #00d9ff; }
.sym-box .label { color: #888; font-size: 11px; }
.sym-box .val   { color: #00d9ff; font-weight: bold; font-size: 14px; }
.sym-box .qty-info { color: #ffd700; }
.btn-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px; }
.btn { padding: 22px 10px; font-size: 17px; font-weight: bold;
       border: none; border-radius: 10px; cursor: pointer; width: 100%; }
.btn:active { opacity: 0.8; transform: scale(0.97); }
.buy-call  { background: #1b5e20; color: #69f0ae; border: 2px solid #4caf50; }
.buy-put   { background: #b71c1c; color: #ff8a80; border: 2px solid #f44336; }
.exit-call { background: #1a237e; color: #82b1ff; border: 2px solid #3f51b5; }
.exit-put  { background: #4a148c; color: #ea80fc; border: 2px solid #9c27b0; }
.exit-all  { width: 100%; padding: 16px; font-size: 16px;
             font-weight: bold; background: #e65100; color: white;
             border: none; border-radius: 10px; cursor: pointer; margin-bottom: 10px; }
.exit-all:active { opacity: 0.8; }
.flash { padding: 10px 14px; border-radius: 8px; margin-bottom: 10px;
         font-size: 13px; text-align: center; font-weight: bold; word-break: break-word; }
.flash.ok  { background: #1b5e20; color: #69f0ae; }
.flash.err { background: #7b1c1c; color: #ff8a80; }
.mkt { font-size: 12px; text-align: center; margin-bottom: 10px; color: #aaa; }
.mkt .open   { color: #4caf50; }
.mkt .closed { color: #f44336; }
.log-box { background: #0a0e27; border-radius: 8px; padding: 10px;
           font-size: 11px; color: #0f0; max-height: 160px;
           overflow-y: auto; font-family: monospace; margin-top: 10px; }
a.back { display: block; text-align: center; margin-top: 14px; color: #00d9ff; font-size: 13px; }
.product-badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
                 font-size: 11px; font-weight: bold; margin-left: 6px; }
.mis-badge  { background: #1b5e20; color: #69f0ae; }
.nrml-badge { background: #7b1c1c; color: #ff8a80; }
.lmt-badge  { background: #1a237e; color: #82b1ff; }
</style>
</head>
<body>

<h1>⚡ Manual Trading
  <span class="product-badge {{ 'mis-badge' if product == 'I' else 'nrml-badge' }}">
    {{ 'MIS' if product == 'I' else 'NRML' }}
  </span>
  <span class="product-badge lmt-badge">
    LMT {{ buffer }}%
  </span>
</h1>

{% if not token %}
<div class="warn-box">
  ❌ Not logged in — <a href="/login" style="color:#ffcdd2">Login first</a>
</div>
{% else %}
<div class="ok-box">
  ✅ Token Active &nbsp;|&nbsp;
  <span style="color:#ffd700">{{ "DRY RUN" if dry else "LIVE" }}</span>
</div>
{% endif %}

<div class="tabs">
  <a href="/manual?inst=NIFTY"     class="tab {{ 'active' if inst=='NIFTY' }}">NIFTY</a>
  <a href="/manual?inst=BANKNIFTY" class="tab {{ 'active' if inst=='BANKNIFTY' }}">BNIFTY</a>
  <a href="/manual?inst=CRUDEOIL"  class="tab {{ 'active' if inst=='CRUDEOIL' }}">CRUDE</a>
</div>

{% if msg %}
<div class="flash {{ 'ok' if msg_ok else 'err' }}">{{ msg }}</div>
{% endif %}

<div class="mkt">
  Market:
  {% if mkt_open %}
    <span class="open">🟢 OPEN</span>
  {% else %}
    <span class="closed">🔴 CLOSED ({{ mkt_open_time }}–{{ mkt_close_time }} IST)</span>
  {% endif %}
  &nbsp;|&nbsp; {{ time_now }}
</div>

<div class="card">
  <h2>⚙️ Strike Setup — {{ inst }}</h2>
  <form method="POST" action="/manual/setup" id="setupForm">
    <input type="hidden" name="inst" value="{{ inst }}">
    <div class="row3">
      <div>
        <label>Day (DD)</label>
        <input name="day" id="fDay" value="{{ fday }}" maxlength="2"
               placeholder="11" oninput="previewSymbol()">
      </div>
      <div>
        <label>Month (MMM)</label>
        <input name="mon" id="fMon" value="{{ fmon }}" maxlength="3"
               placeholder="AUG" oninput="previewSymbol()" style="text-transform:uppercase">
      </div>
      <div>
        <label>Year (YY)</label>
        <input name="yr" id="fYr" value="{{ fyr }}" maxlength="2"
               placeholder="26" oninput="previewSymbol()">
      </div>
    </div>
    <div class="row2">
      <div>
        <label>CE Strike</label>
        <input name="ce" id="fCe" value="{{ fce }}"
               placeholder="24500" oninput="previewSymbol()">
      </div>
      <div>
        <label>PE Strike</label>
        <input name="pe" id="fPe" value="{{ fpe }}"
               placeholder="same as CE" oninput="previewSymbol()">
      </div>
    </div>
    <div class="row2">
      <div>
        <label>Lots</label>
        <input name="lots" id="fLots" type="number" value="{{ flots }}"
               min="1" oninput="previewSymbol()">
      </div>
      <div>
        <label>Qty</label>
        <input id="fQtyDisplay" disabled style="color:#ffd700"
               value="{{ flots|int * lot_size }}">
      </div>
    </div>
    <div style="margin-top:10px;padding:8px;background:#0a0e27;border-radius:6px;font-size:12px;">
      <span style="color:#888">Preview: </span>
      <span id="previewCe" style="color:#69f0ae">-</span>
      &nbsp;/&nbsp;
      <span id="previewPe" style="color:#ff8a80">-</span>
    </div>
    <button type="submit" class="save-btn">💾 Save & Activate Strikes</button>
  </form>
</div>

{% if call_sym and put_sym %}
<div class="sym-box">
  <div class="label">🟢 ACTIVE — Orders will use these symbols:</div>
  <div>CE: <span class="val">{{ call_sym }}</span></div>
  <div>PE: <span class="val">{{ put_sym }}</span></div>
  <div class="qty-info">📦 Qty: {{ qty }} &nbsp;({{ saved_lots }} lot × {{ lot_size }})</div>
</div>

<div class="btn-grid">
  <form method="POST" action="/manual/order">
    <input type="hidden" name="inst"   value="{{ inst }}">
    <input type="hidden" name="action" value="BUY_CALL">
    <button type="submit" class="btn buy-call">📈 BUY<br>CALL</button>
  </form>
  <form method="POST" action="/manual/order">
    <input type="hidden" name="inst"   value="{{ inst }}">
    <input type="hidden" name="action" value="BUY_PUT">
    <button type="submit" class="btn buy-put">📉 BUY<br>PUT</button>
  </form>
  <form method="POST" action="/manual/order">
    <input type="hidden" name="inst"   value="{{ inst }}">
    <input type="hidden" name="action" value="EXIT_CALL">
    <button type="submit" class="btn exit-call">🚪 EXIT<br>CALL</button>
  </form>
  <form method="POST" action="/manual/order">
    <input type="hidden" name="inst"   value="{{ inst }}">
    <input type="hidden" name="action" value="EXIT_PUT">
    <button type="submit" class="btn exit-put">🚪 EXIT<br>PUT</button>
  </form>
</div>

<form method="POST" action="/manual/order">
  <input type="hidden" name="inst"   value="{{ inst }}">
  <input type="hidden" name="action" value="EXIT_ALL">
  <button type="submit" class="exit-all">⛔ EXIT ALL (CE + PE)</button>
</form>

{% else %}
<div class="warn-box" style="margin-top:10px;">
  ⚠️ Fill strike details above and tap <b>Save & Activate Strikes</b> to enable trading buttons.
</div>
{% endif %}

{% if logs %}
<h2 style="margin-top:14px;">📋 Recent Activity</h2>
<div class="log-box">{% for l in logs %}{{ l }}<br>{% endfor %}</div>
{% endif %}

<a href="/" class="back">← Back to Dashboard</a>

<script>
const inst    = "{{ inst }}";
const lotSize = {{ lot_size }};

function previewSymbol() {
  const day  = document.getElementById('fDay').value.trim().padStart(2,'0');
  const mon  = document.getElementById('fMon').value.trim().toUpperCase();
  const yr   = document.getElementById('fYr').value.trim();
  const ce   = document.getElementById('fCe').value.trim();
  const pe   = document.getElementById('fPe').value.trim() || ce;
  const lots = parseInt(document.getElementById('fLots').value) || 1;

  document.getElementById('fQtyDisplay').value =
    (lots * lotSize) + ' (' + lots + '×' + lotSize + ')';

  if (day && mon && yr && ce) {
    const expiry = day + mon + yr;
    document.getElementById('previewCe').textContent = inst + expiry + 'C' + ce;
    document.getElementById('previewPe').textContent = inst + expiry + 'P' + pe;
  } else {
    document.getElementById('previewCe').textContent = '-';
    document.getElementById('previewPe').textContent = '-';
  }
}
previewSymbol();
</script>
</body>
</html>
"""

# =============================================
# ROUTES
# =============================================
@app.route("/")
def home():
    market_status = {name: is_market_open_for(name) for name in INSTRUMENTS}
    return render_template_string(HOME_HTML,
        token=state["token"], dry=DRY_RUN,
        product=PRODUCT,
        order_type=ORDER_TYPE,
        buffer=LMT_BUFFER_PCT,
        self_url=SELF_URL,
        instruments=INSTRUMENTS,
        market_status=market_status,
        time_now=datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        host=request.host)

@app.route("/login", methods=["GET", "POST"])
def login():
    msg = None; success = False
    if request.method == "POST":
        code = request.form.get("request_code", "").strip()
        if code:
            success, result = exchange_request_code(code)
            msg = "✅ Token generated!" if success else f"❌ {result}"
    return render_template_string(LOGIN_HTML, msg=msg, success=success, api_key=API_KEY)

@app.route("/logs")
def logs_page():
    lines = "\n".join(reversed(state["logs"]))
    return (
        f"<pre style='background:#000;color:#0f0;"
        f"padding:20px;font-size:13px;'>{lines}</pre>"
    )

@app.route("/ping")
def ping():
    return jsonify({"status": "alive", "time": datetime.now(IST).strftime("%H:%M:%S")}), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"status": "error", "msg": "empty"}), 400

        add_log(f"[WEBHOOK] {json.dumps(data)}")

        if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
            add_log("[WEBHOOK] Secret mismatch.")
            return jsonify({"status": "error", "msg": "unauthorized"}), 403

        action            = data.get("action", "")
        qty_raw           = data.get("qty", None)
        symbol_from_alert = data.get("symbol", None)

        qty = None
        if qty_raw is not None:
            try:
                qty = int(str(qty_raw).split(".")[0])
            except Exception:
                qty = None

        threading.Thread(
            target=handle_action,
            args=(action, qty, symbol_from_alert),
            daemon=True
        ).start()

        return jsonify({"status": "ok", "action": action,
                        "qty": qty, "symbol": symbol_from_alert}), 200

    except Exception as e:
        add_log(f"[WEBHOOK] Exception: {e}")
        return jsonify({"status": "error", "msg": str(e)}), 500

@app.route("/health")
def health():
    market_status = {name: is_market_open_for(name) for name in INSTRUMENTS}
    return jsonify({
        "status":     "running",
        "dry_run":    DRY_RUN,
        "product":    PRODUCT,
        "order_type": ORDER_TYPE,
        "lmt_buffer": LMT_BUFFER_PCT,
        "token_ok":   bool(state["token"]),
        "keep_alive": bool(SELF_URL),
        "instruments": {
            name: {"exchange":    cfg["exchange"],
                   "lot_size":    cfg["lot_size"],
                   "market_open": market_status[name]}
            for name, cfg in INSTRUMENTS.items()
        },
        "time_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    }), 200

# =============================================
# MANUAL ROUTES
# =============================================
@app.route("/manual", methods=["GET"])
def manual():
    inst = request.args.get("inst", "NIFTY").upper()
    if inst not in INSTRUMENTS:
        inst = "NIFTY"

    cfg      = INSTRUMENTS[inst]
    m        = state["manual"][inst]
    f        = state["form"][inst]
    now_ist  = datetime.now(IST)
    lot_size = cfg["lot_size"]

    fday  = f["day"]       or now_ist.strftime("%d")
    fmon  = f["mon"]       or now_ist.strftime("%b").upper()
    fyr   = f["yr"]        or now_ist.strftime("%y")
    fce   = f["ce_strike"]
    fpe   = f["pe_strike"]
    flots = f["lots"]

    msg    = request.args.get("msg", "")
    msg_ok = request.args.get("ok", "1") == "1"

    recent_logs = [
        l for l in reversed(state["logs"])
        if any(k in l for k in [
            "[ACTION]", "[ORDER]", "[EXIT]", "[OK]", "[ERR]", "MANUAL", "DRY RUN"
        ])
    ][:15]

    return render_template_string(MANUAL_HTML,
        inst       = inst,
        token      = state["token"],
        dry        = DRY_RUN,
        product    = PRODUCT,
        buffer     = LMT_BUFFER_PCT,
        call_sym   = m["call"],
        put_sym    = m["put"],
        qty        = m["qty"],
        saved_lots = m["lots"],
        lot_size   = lot_size,
        fday=fday, fmon=fmon, fyr=fyr,
        fce=fce,   fpe=fpe,   flots=flots,
        mkt_open       = is_market_open_for(inst),
        mkt_open_time  = cfg["market_open"].strftime("%H:%M"),
        mkt_close_time = cfg["market_close"].strftime("%H:%M"),
        time_now       = now_ist.strftime("%H:%M:%S"),
        msg    = msg,
        msg_ok = msg_ok,
        logs   = recent_logs,
    )

@app.route("/manual/setup", methods=["POST"])
def manual_setup():
    inst = request.form.get("inst", "NIFTY").upper()
    if inst not in INSTRUMENTS:
        inst = "NIFTY"

    cfg      = INSTRUMENTS[inst]
    lot_size = cfg["lot_size"]

    day  = request.form.get("day",  "").strip().zfill(2)
    mon  = request.form.get("mon",  "").strip().upper()
    yr   = request.form.get("yr",   "").strip()
    ce   = request.form.get("ce",   "").strip()
    pe   = request.form.get("pe",   "").strip()
    lots = max(1, int(request.form.get("lots", "1") or "1"))

    if not all([day, mon, yr, ce]):
        return redirect_manual(inst, "❌ Fill Day/Mon/Yr/CE!", ok=False)

    pe_final = pe if pe else ce

    state["form"][inst] = {
        "day":       day,
        "mon":       mon,
        "yr":        yr,
        "ce_strike": ce,
        "pe_strike": pe,
        "lots":      str(lots)
    }

    expiry   = f"{day}{mon}{yr}"
    call_sym = f"{inst}{expiry}C{ce}"
    put_sym  = f"{inst}{expiry}P{pe_final}"
    qty      = lots * lot_size

    state["manual"][inst] = {
        "call": call_sym,
        "put":  put_sym,
        "qty":  qty,
        "lots": lots
    }

    add_log(f"[MANUAL] {inst} | NEW STRIKES SAVED | CE={call_sym} PE={put_sym} QTY={qty}")

    return redirect_manual(
        inst,
        f"✅ NEW Strikes Active! CE={call_sym} | PE={put_sym} | Qty={qty}"
    )

@app.route("/manual/order", methods=["POST"])
def manual_order():
    inst   = request.form.get("inst",   "NIFTY").upper()
    action = request.form.get("action", "").upper()

    if inst not in INSTRUMENTS:
        return redirect_manual("NIFTY", "❌ Unknown instrument", ok=False)

    if not state["token"]:
        return redirect_manual(inst, "❌ Not logged in. Visit /login", ok=False)

    m = state["manual"][inst]
    if not m["call"] or not m["put"]:
        return redirect_manual(inst, "❌ Set strikes first!", ok=False)

    ce  = m["call"]
    pe  = m["put"]
    qty = m["qty"]

    add_log(f"[MANUAL] {inst} | {action} | CE={ce} PE={pe} QTY={qty}")

    result_holder = {"msg": "", "ok": True}

    def run():
        try:
            if action == "BUY_CALL":
                r = place_order(ce, "B", qty)
                result_holder["msg"] = (f"✅ BUY CALL sent | {ce} qty={qty}"
                                        if r else "❌ BUY CALL failed | Check logs")
                result_holder["ok"] = bool(r)
            elif action == "BUY_PUT":
                r = place_order(pe, "B", qty)
                result_holder["msg"] = (f"✅ BUY PUT sent | {pe} qty={qty}"
                                        if r else "❌ BUY PUT failed | Check logs")
                result_holder["ok"] = bool(r)
            elif action == "EXIT_CALL":
                r = exit_position(ce)
                result_holder["msg"] = r or "✅ EXIT CALL done"
                result_holder["ok"]  = r is not None and "ERROR" not in r and "failed" not in r.lower()
            elif action == "EXIT_PUT":
                r = exit_position(pe)
                result_holder["msg"] = r or "✅ EXIT PUT done"
                result_holder["ok"]  = r is not None and "ERROR" not in r and "failed" not in r.lower()
            elif action == "EXIT_ALL":
                r1 = exit_position(ce)
                r2 = exit_position(pe)
                result_holder["msg"] = f"CE→ {r1 or 'done'} | PE→ {r2 or 'done'}"
                result_holder["ok"]  = all(x is not None and "ERROR" not in x for x in [r1, r2])
            else:
                result_holder["msg"] = f"❌ Unknown: {action}"
                result_holder["ok"]  = False
        except Exception as e:
            result_holder["msg"] = f"❌ Error: {e}"
            result_holder["ok"]  = False
            add_log(f"[MANUAL] Exception: {e}")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=20)

    return redirect_manual(inst, result_holder["msg"] or "✅ Done", ok=result_holder["ok"])


def redirect_manual(inst, msg, ok=True):
    return redirect(f"/manual?inst={inst}&msg={quote(str(msg))}&ok={'1' if ok else '0'}")


# =============================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
