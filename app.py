"""
Flattrade MULTI-INSTRUMENT - CLOUD VERSION for Railway v3
=========================================================
Supports: NIFTY, BANKNIFTY, CRUDEOIL
Auto-detects instrument + CE/PE from symbol in alert
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
from flask import Flask, request, jsonify, render_template_string
import pytz

# =============================================
USER_ID        = os.environ.get("USER_ID", "")
API_KEY        = os.environ.get("API_KEY", "")
API_SECRET     = os.environ.get("API_SECRET", "")
PROXY_HOST     = os.environ.get("PROXY_HOST", "")
PROXY_PORT     = os.environ.get("PROXY_PORT", "443")
PROXY_USER     = os.environ.get("PROXY_USER", "")
PROXY_PASS     = os.environ.get("PROXY_PASS", "")
PRODUCT        = os.environ.get("PRODUCT", "M")
DRY_RUN        = os.environ.get("DRY_RUN", "False").lower() == "true"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

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

# Default fallback if instrument can't be detected
DEFAULT_LOT_SIZE = 65
# =============================================

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

state = {
    "token":       None,
    "call_symbol": "",
    "put_symbol":  "",
    "instrument":  "NIFTY",  # for /setup page default
    "qty":         DEFAULT_LOT_SIZE,
    "logs":        [],
    "last_action": {},
    # Manual trading state (per instrument)
    "manual": {
        "NIFTY":     {"call": "", "put": "", "qty": 65,  "lots": 1},
        "BANKNIFTY": {"call": "", "put": "", "qty": 30,  "lots": 1},
        "CRUDEOIL":  {"call": "", "put": "", "qty": 100, "lots": 1},
    }
}

IST = pytz.timezone("Asia/Kolkata")

def add_log(msg):
    ts = datetime.now(IST).strftime("%H:%M:%S")
    state["logs"].append(f"[{ts}] {msg}")
    state["logs"] = state["logs"][-200:]
    log.info(msg)

# -----------------------------------------------
# INSTRUMENT DETECTION FROM SYMBOL
# -----------------------------------------------
def detect_instrument(symbol):
    """Returns instrument name from symbol prefix"""
    if not symbol:
        return None
    symbol = symbol.upper()
    # Check longest prefix first (BANKNIFTY before NIFTY)
    if symbol.startswith("BANKNIFTY"):
        return "BANKNIFTY"
    if symbol.startswith("CRUDEOIL"):
        return "CRUDEOIL"
    if symbol.startswith("NIFTY"):
        return "NIFTY"
    return None

def detect_option_type(symbol):
    """Returns 'CE' or 'PE' from symbol"""
    if not symbol:
        return None
    symbol = symbol.upper()
    if re.search(r'C\d+$', symbol):
        return 'CE'
    if re.search(r'P\d+$', symbol):
        return 'PE'
    return None

def get_instrument_config(symbol):
    """Get exchange, lot_size, market hours for given symbol"""
    instrument = detect_instrument(symbol)
    if instrument and instrument in INSTRUMENTS:
        return instrument, INSTRUMENTS[instrument]
    return None, None

def is_market_open_for(instrument):
    """Check market hours for specific instrument"""
    if instrument not in INSTRUMENTS:
        return False
    cfg = INSTRUMENTS[instrument]
    now = datetime.now(IST).time()
    return cfg["market_open"] <= now <= cfg["market_close"]

# -----------------------------------------------
# TOKEN
# -----------------------------------------------
def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()

def exchange_request_code(request_code):
    hash_value = sha256(API_KEY + request_code + API_SECRET)
    payload = {"api_key": API_KEY, "request_code": request_code, "api_secret": hash_value}
    try:
        r = requests.post(TOKEN_URL, json=payload, proxies=PROXIES, timeout=15)
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

# -----------------------------------------------
# API CALLS (now instrument-aware)
# -----------------------------------------------
def get_token_for_symbol(symbol, exchange):
    try:
        r = requests.post(BASE_URL_ORDER + "/SearchScrip",
            data="jData=" + json.dumps({"uid": USER_ID, "stext": symbol, "exch": exchange}) +
                 "&jKey=" + state["token"],
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            proxies=PROXIES, timeout=8)
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
        r = requests.post(BASE_URL_ORDER + "/GetQuotes",
            data="jData=" + json.dumps({"uid": USER_ID, "exch": exchange, "token": tk}) +
                 "&jKey=" + state["token"],
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            proxies=PROXIES, timeout=8)
        resp = r.json()
        if resp.get("stat") == "Ok":
            ltp = float(resp.get("lp", 0))
            add_log(f"[LTP] {symbol} = {ltp}")
            return ltp
    except Exception as e:
        add_log(f"[LTP] Exception: {e}")
    return None

def place_order(symbol, trantype, qty=None):
    action = "BUY" if trantype == "B" else "SELL"

    # Auto-detect instrument config
    instrument, cfg = get_instrument_config(symbol)
    if not cfg:
        add_log(f"[ORDER] ERROR: Unknown instrument for symbol {symbol}")
        return None

    exchange = cfg["exchange"]
    qty = qty or cfg["lot_size"]

    add_log(f"[ORDER] {action} → {symbol} | qty={qty} | {instrument} ({exchange})")

    if not state["token"]:
        add_log("[ORDER] ERROR: No token. Visit /login first!")
        return None

    if not DRY_RUN and not is_market_open_for(instrument):
        oh = cfg["market_open"].strftime("%H:%M")
        ch = cfg["market_close"].strftime("%H:%M")
        add_log(f"[ORDER] {instrument} market CLOSED ({oh}-{ch} IST)")
        return None

    ltp = get_ltp(symbol, exchange)
    if ltp is None:
        add_log("[ORDER] LTP fetch failed. Aborting.")
        return None

    price = round(ltp * 1.005, 1) if trantype == "B" else round(ltp * 0.995, 1)

    if DRY_RUN:
        add_log(f"[DRY RUN] {action} {qty} {symbol} @ {price} (LTP {ltp})")
        return "DRY_RUN"

    payload = {
        "uid": USER_ID, "actid": USER_ID,
        "exch": exchange, "tsym": symbol,
        "qty": str(qty), "prc": str(price),
        "prd": PRODUCT, "trantype": trantype,
        "prctyp": "LMT", "ret": "DAY"
    }
    try:
        r = requests.post(BASE_URL_ORDER + "/PlaceOrder",
            data="jData=" + json.dumps(payload) + "&jKey=" + state["token"],
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            proxies=PROXIES, timeout=8)
        resp = r.json()
        if resp.get("stat") == "Ok":
            ordno = resp.get("norenordno", "")
            add_log(f"[OK] ORDER PLACED | {action} {qty} {symbol} @ {price} | No: {ordno}")
            return ordno
        add_log(f"[ERR] ORDER FAILED | {resp.get('emsg')}")
    except Exception as e:
        add_log(f"[ERR] Exception: {e}")
    return None

def exit_position(symbol, qty=None):
    add_log(f"[EXIT] Triggered for {symbol}")

    instrument, cfg = get_instrument_config(symbol)
    if not cfg:
        add_log(f"[EXIT] ERROR: Unknown instrument for {symbol}")
        return

    exchange = cfg["exchange"]

    if not state["token"]:
        add_log("[EXIT] ERROR: No token. Visit /login first!")
        return

    if not DRY_RUN and not is_market_open_for(instrument):
        oh = cfg["market_open"].strftime("%H:%M")
        ch = cfg["market_close"].strftime("%H:%M")
        add_log(f"[EXIT] {instrument} market CLOSED ({oh}-{ch} IST)")
        return

    if DRY_RUN:
        add_log(f"[DRY RUN] Would exit {symbol}")
        return

    try:
        r = requests.post(BASE_URL_ORDER + "/PositionBook",
            data="jData=" + json.dumps({"uid": USER_ID, "actid": USER_ID}) +
                 "&jKey=" + state["token"],
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            proxies=PROXIES, timeout=8)
        positions = r.json()
        if not isinstance(positions, list):
            add_log(f"[EXIT] No position list: {positions}")
            return

        found = False
        for pos in positions:
            if pos.get("tsym") != symbol:
                continue
            found = True
            net_qty = int(pos.get("netqty", 0))
            if net_qty == 0:
                add_log(f"[EXIT] No open qty in {symbol}")
                return

            trantype = "S" if net_qty > 0 else "B"
            exit_qty = abs(net_qty)
            ltp = get_ltp(symbol, exchange) or float(pos.get("lp", 100))
            price = round(ltp * 0.995, 1) if trantype == "S" else round(ltp * 1.005, 1)

            sq_payload = {
                "uid": USER_ID, "actid": USER_ID,
                "exch": exchange, "tsym": symbol,
                "qty": str(exit_qty), "prc": str(price),
                "prd": PRODUCT, "trantype": trantype,
                "prctyp": "LMT", "ret": "DAY"
            }
            sq_r = requests.post(BASE_URL_ORDER + "/PlaceOrder",
                data="jData=" + json.dumps(sq_payload) + "&jKey=" + state["token"],
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                proxies=PROXIES, timeout=8)
            sq_resp = sq_r.json()
            if sq_resp.get("stat") == "Ok":
                add_log(f"[OK] EXITED {symbol} qty {exit_qty} | No: {sq_resp.get('norenordno')}")
            else:
                add_log(f"[ERR] EXIT FAILED | {sq_resp.get('emsg')}")

        if not found:
            add_log(f"[EXIT] {symbol} not in positions.")
    except Exception as e:
        add_log(f"[EXIT] Exception: {e}")

# -----------------------------------------------
# ACTION HANDLER
# -----------------------------------------------
def handle_action(action, qty=None, symbol=None):
    action = action.upper().strip()

    # Debounce
    dedup_key = f"{action}_{symbol or ''}"
    now = time.time()
    if now - state["last_action"].get(dedup_key, 0) < 2:
        add_log(f"[DEBOUNCE] Blocked duplicate: {action} {symbol or ''}")
        return
    state["last_action"][dedup_key] = now

    # ---- Dynamic: symbol from TradingView alert ----
    if symbol:
        symbol = symbol.strip().upper()
        instrument = detect_instrument(symbol)
        opt_type = detect_option_type(symbol)

        # Use instrument's lot size if qty not specified
        if not qty and instrument in INSTRUMENTS:
            qty = INSTRUMENTS[instrument]["lot_size"]
        elif not qty:
            qty = DEFAULT_LOT_SIZE

        add_log(f"[ACTION] {action} on {symbol} | Instrument: {instrument} | Type: {opt_type} | Qty: {qty}")

        if action in ("BUY", "BUY_CALL", "BUY_PUT"):
            place_order(symbol, "B", qty)
        elif action in ("SELL", "EXIT", "EXIT_CALL", "EXIT_PUT", "EXIT_ALL"):
            exit_position(symbol, qty)
        elif action in ("SELL_SHORT", "SHORT"):
            place_order(symbol, "S", qty)
        else:
            add_log(f"[ACTION] Unknown action: {action}")
        return

    # ---- Fallback: use pre-configured CE/PE from /setup ----
    ce = state["call_symbol"]
    pe = state["put_symbol"]
    qty = qty or state["qty"]

    if not ce or not pe:
        add_log(f"[ACTION] ERROR: No symbol in alert AND strikes not set at /setup!")
        return

    action_map = {
        "BUY_CALL":  lambda: place_order(ce, "B", qty),
        "BUY_PUT":   lambda: place_order(pe, "B", qty),
        "EXIT_CALL": lambda: exit_position(ce, qty),
        "EXIT_PUT":  lambda: exit_position(pe, qty),
        "EXIT_ALL":  lambda: (exit_position(ce, qty), exit_position(pe, qty)),
        "SELL_CALL": lambda: place_order(ce, "S", qty),
        "SELL_PUT":  lambda: place_order(pe, "S", qty),
    }

    fn = action_map.get(action)
    if fn:
        fn()
    else:
        add_log(f"[ACTION] Unknown: {action}")

# -----------------------------------------------
# FLASK APP
# -----------------------------------------------
app = Flask(__name__)

HOME_HTML = """
<!DOCTYPE html><html><head><title>Flattrade Multi-Instrument</title>
<style>
body{font-family:Arial;background:#1a1a2e;color:#eee;padding:30px;max-width:900px;margin:auto;}
h1{color:#00d9ff;}h2{color:#ffd700;margin-top:30px;}
.status{padding:15px;background:#16213e;border-radius:8px;margin:10px 0;}
.ok{color:#4caf50;}.err{color:#f44336;}.warn{color:#ff9800;}
a{color:#00d9ff;text-decoration:none;padding:10px 20px;background:#0f3460;
  border-radius:5px;display:inline-block;margin:5px;}
a:hover{background:#e94560;}
table{width:100%;border-collapse:collapse;background:#16213e;border-radius:8px;overflow:hidden;}
th,td{padding:10px;text-align:left;border-bottom:1px solid #0f3460;}
th{background:#0f3460;color:#ffd700;}
code{background:#0a0e27;padding:3px 6px;border-radius:3px;color:#00d9ff;}
</style></head><body>
<h1>🚀 Flattrade Algo — Multi-Instrument v3</h1>

<div class="status">
<b>Token:</b> <span class="{{'ok' if token else 'err'}}">{{'✅ Active' if token else '❌ Not logged in'}}</span><br>
<b>Mode:</b> <span class="{{'warn' if dry else 'ok'}}">{{'DRY RUN' if dry else 'LIVE'}}</span><br>
<b>Server Time (IST):</b> {{time_now}}
</div>

<h2>📊 Supported Instruments</h2>
<table>
<tr><th>Instrument</th><th>Exchange</th><th>Lot Size</th><th>Market Hours (IST)</th><th>Status</th></tr>
{% for name, cfg in instruments.items() %}
<tr>
<td><b>{{name}}</b></td>
<td>{{cfg.exchange}}</td>
<td>{{cfg.lot_size}}</td>
<td>{{cfg.market_open.strftime('%H:%M')}} - {{cfg.market_close.strftime('%H:%M')}}</td>
<td>{% if market_status[name] %}<span class="ok">🟢 OPEN</span>{% else %}<span class="warn">🔴 CLOSED</span>{% endif %}</td>
</tr>
{% endfor %}
</table>

<h2>Quick Actions</h2>
<a href="/login">🔑 Daily Login</a>
<a href="/manual">⚡ Manual Trading</a>
<a href="/setup">⚙️ Strike Setup (Optional)</a>
<a href="/logs">📋 View Logs</a>
<a href="/health">💚 Health</a>

<h2>🔗 Webhook URL for TradingView (USE HTTPS!)</h2>
<div class="status"><code>https://{{host}}/webhook</code></div>

<h2>📝 Alert JSON Examples</h2>
<div class="status">
<b>NIFTY:</b><br>
<code>{"action":"BUY","symbol":"NIFTY11AUG26C24500","qty":"65"}</code><br>
<code>{"action":"SELL","symbol":"NIFTY11AUG26P24050","qty":"65"}</code><br><br>
<b>BANKNIFTY:</b><br>
<code>{"action":"BUY","symbol":"BANKNIFTY27AUG26C51500","qty":"30"}</code><br>
<code>{"action":"SELL","symbol":"BANKNIFTY27AUG26P51000","qty":"30"}</code><br><br>
<b>CRUDEOIL:</b><br>
<code>{"action":"BUY","symbol":"CRUDEOIL20AUG26C6500","qty":"100"}</code><br>
<code>{"action":"SELL","symbol":"CRUDEOIL20AUG26P6300","qty":"100"}</code>
</div>

<p style="color:#888;font-size:12px;margin-top:30px;">
💡 Instrument auto-detected from symbol prefix. Lot size auto-applied. Market hours auto-checked.
</p>
</body></html>
"""

@app.route("/")
def home():
    market_status = {name: is_market_open_for(name) for name in INSTRUMENTS}
    return render_template_string(HOME_HTML,
        token=state["token"], dry=DRY_RUN,
        instruments=INSTRUMENTS, market_status=market_status,
        time_now=datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        host=request.host)

LOGIN_HTML = """
<!DOCTYPE html><html><head><title>Login</title>
<style>body{font-family:Arial;background:#1a1a2e;color:#eee;padding:30px;max-width:700px;margin:auto;}
h1{color:#00d9ff;}a.btn,button{background:#e94560;color:white;padding:12px 25px;border:none;
  border-radius:5px;text-decoration:none;font-size:16px;cursor:pointer;display:inline-block;}
input{width:100%;padding:10px;font-size:16px;background:#0f3460;color:white;border:1px solid #00d9ff;
  border-radius:5px;margin:10px 0;box-sizing:border-box;}
.step{margin:20px 0;padding:15px;background:#0f3460;border-left:4px solid #00d9ff;}
.msg{padding:15px;border-radius:5px;margin:15px 0;}
.ok{background:#1b5e20;}.err{background:#b71c1c;}
</style></head><body>
<h1>🔑 Daily Flattrade Login</h1>
{% if msg %}<div class="msg {{'ok' if success else 'err'}}">{{msg}}</div>{% endif %}
<div class="step"><b>Step 1:</b><br><br>
<a class="btn" href="https://auth.flattrade.in/?app_key={{api_key}}" target="_blank">🚀 Login to Flattrade</a>
</div>
<div class="step"><b>Step 2:</b> Paste request_code from redirect URL:</div>
<form method="POST"><input name="request_code" placeholder="Paste request_code" required autofocus>
<button type="submit">Generate Token</button></form>
<br><a href="/" style="color:#00d9ff">← Back</a>
</body></html>
"""

@app.route("/login", methods=["GET", "POST"])
def login():
    msg = None; success = False
    if request.method == "POST":
        code = request.form.get("request_code", "").strip()
        if code:
            success, result = exchange_request_code(code)
            msg = "✅ Token generated!" if success else f"❌ {result}"
    return render_template_string(LOGIN_HTML, msg=msg, success=success, api_key=API_KEY)

SETUP_HTML = """
<!DOCTYPE html><html><head><title>Strike Setup</title>
<style>body{font-family:Arial;background:#1a1a2e;color:#eee;padding:30px;max-width:600px;margin:auto;}
h1{color:#00d9ff;}.box{background:#16213e;padding:20px;border-radius:8px;}
label{display:block;margin:15px 0 5px;color:#ffd700;}
input,select{width:100%;padding:10px;font-size:16px;background:#0f3460;color:white;
  border:1px solid #00d9ff;border-radius:5px;box-sizing:border-box;}
button{background:#e94560;color:white;padding:12px 25px;border:none;border-radius:5px;
  font-size:16px;cursor:pointer;margin-top:20px;}
.ok{background:#1b5e20;padding:15px;border-radius:5px;margin:15px 0;}
.note{background:#5d4037;padding:10px;border-radius:5px;margin:15px 0;font-size:14px;}
</style></head><body>
<h1>⚙️ Strike Setup (Optional Fallback)</h1>
<div class="note">💡 <b>Note:</b> If your TradingView alert already sends the "symbol" field,
this setup is NOT needed. Use this only for old-style alerts.</div>
{% if saved %}<div class="ok">✅ Saved!<br>Instrument: {{instrument}}<br>
CE: {{ce}}<br>PE: {{pe}}<br>Qty: {{qty}}</div>{% endif %}
<div class="box"><form method="POST">
<label>Instrument:</label>
<select name="instrument" required>
<option value="NIFTY" {% if instrument=='NIFTY' %}selected{% endif %}>NIFTY (Lot 65)</option>
<option value="BANKNIFTY" {% if instrument=='BANKNIFTY' %}selected{% endif %}>BANKNIFTY (Lot 30)</option>
<option value="CRUDEOIL" {% if instrument=='CRUDEOIL' %}selected{% endif %}>CRUDEOIL (Lot 100)</option>
</select>
<label>Day (DD):</label><input name="day" value="{{day}}" required>
<label>Month (JAN..DEC):</label><input name="mon" value="{{mon}}" required>
<label>Year (YY):</label><input name="yr" value="{{yr}}" required>
<label>CE Strike:</label><input name="ce_strike" value="{{ce_strike}}" required>
<label>PE Strike (blank = same as CE):</label><input name="pe_strike" value="{{pe_strike}}">
<label>Lots:</label><input name="lots" type="number" value="{{lots}}" required>
<button type="submit">Save Strikes</button></form></div>
<br><a href="/" style="color:#00d9ff">← Back</a>
</body></html>
"""

@app.route("/setup", methods=["GET", "POST"])
def setup():
    now_ist = datetime.now(IST)
    defaults = {"day": now_ist.strftime("%d"), "mon": now_ist.strftime("%b").upper(),
                "yr": now_ist.strftime("%y"), "ce_strike": "24500",
                "pe_strike": "", "lots": "1", "instrument": state.get("instrument", "NIFTY")}
    saved = False
    if request.method == "POST":
        instrument = request.form.get("instrument", "NIFTY").upper()
        day = request.form.get("day", "").strip().zfill(2)
        mon = request.form.get("mon", "").strip().upper()
        yr  = request.form.get("yr", "").strip()
        ce_strike = request.form.get("ce_strike", "").strip()
        pe_strike = request.form.get("pe_strike", "").strip() or ce_strike
        lots = int(request.form.get("lots", "1"))

        expiry = f"{day}{mon}{yr}"
        state["call_symbol"] = f"{instrument}{expiry}C{ce_strike}"
        state["put_symbol"]  = f"{instrument}{expiry}P{pe_strike}"
        state["instrument"] = instrument

        lot_size = INSTRUMENTS.get(instrument, {}).get("lot_size", DEFAULT_LOT_SIZE)
        state["qty"] = lots * lot_size

        add_log(f"[SETUP] {instrument} | CE={state['call_symbol']} PE={state['put_symbol']} QTY={state['qty']}")
        saved = True
        defaults.update({"day": day, "mon": mon, "yr": yr,
                         "ce_strike": ce_strike, "pe_strike": pe_strike,
                         "lots": lots, "instrument": instrument})

    return render_template_string(SETUP_HTML, saved=saved,
        ce=state["call_symbol"], pe=state["put_symbol"], qty=state["qty"], **defaults)

@app.route("/logs")
def logs():
    lines = "\n".join(state["logs"][::-1])
    return f"<pre style='background:#000;color:#0f0;padding:20px;font-size:13px;'>{lines}</pre>"

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"status": "error", "msg": "empty payload"}), 400

        add_log(f"[WEBHOOK] {json.dumps(data)}")

        if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
            add_log("[WEBHOOK] Secret mismatch. Rejected.")
            return jsonify({"status": "error", "msg": "unauthorized"}), 403

        action = data.get("action", "")
        qty_raw = data.get("qty", None)
        symbol_from_alert = data.get("symbol", None)

        qty = None
        if qty_raw is not None:
            try:
                qty = int(str(qty_raw).split(".")[0])
            except Exception:
                qty = None

        t = threading.Thread(target=handle_action,
                             args=(action, qty, symbol_from_alert), daemon=True)
        t.start()

        return jsonify({"status": "ok", "action": action, "qty": qty,
                        "symbol": symbol_from_alert}), 200
    except Exception as e:
        add_log(f"[WEBHOOK] Exception: {e}")
        return jsonify({"status": "error", "msg": str(e)}), 500

@app.route("/health")
def health():
    market_status = {name: is_market_open_for(name) for name in INSTRUMENTS}
    return jsonify({
        "status": "running", "dry_run": DRY_RUN,
        "ce": state["call_symbol"], "pe": state["put_symbol"],
        "qty": state["qty"], "token_ok": bool(state["token"]),
        "instruments": {name: {"exchange": cfg["exchange"],
                               "lot_size": cfg["lot_size"],
                               "market_open": market_status[name]}
                        for name, cfg in INSTRUMENTS.items()},
        "time_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    }), 200

MANUAL_HTML = """
<!DOCTYPE html>
<html>
<head>
<title>Manual Trading</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: Arial, sans-serif; background: #1a1a2e; color: #eee;
       padding: 12px; max-width: 480px; margin: auto; }
h1 { color: #00d9ff; font-size: 20px; margin-bottom: 10px; text-align: center; }
h2 { color: #ffd700; font-size: 15px; margin: 14px 0 6px; }

/* Token warning */
.warn-box { background: #7b1c1c; border-radius: 8px; padding: 10px 14px;
            margin-bottom: 12px; font-size: 13px; text-align: center; }
.ok-box   { background: #1b5e20; border-radius: 8px; padding: 8px 14px;
            margin-bottom: 12px; font-size: 13px; text-align: center; }

/* Tabs */
.tabs { display: flex; gap: 6px; margin-bottom: 14px; }
.tab  { flex: 1; padding: 10px 4px; text-align: center; border-radius: 8px;
        background: #0f3460; color: #aaa; font-size: 13px; font-weight: bold;
        cursor: pointer; border: 2px solid transparent; text-decoration: none; }
.tab.active { background: #e94560; color: white; border-color: #ff6b6b; }

/* Setup form */
.card { background: #16213e; border-radius: 10px; padding: 14px; margin-bottom: 14px; }
label { display: block; color: #ffd700; font-size: 12px; margin: 8px 0 3px; }
input, select { width: 100%; padding: 9px 10px; font-size: 15px;
                background: #0f3460; color: white; border: 1px solid #00d9ff;
                border-radius: 6px; }
.row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.row3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
.save-btn { width: 100%; margin-top: 12px; padding: 13px; font-size: 16px;
            background: #00d9ff; color: #000; border: none; border-radius: 8px;
            font-weight: bold; cursor: pointer; }
.save-btn:active { background: #0099bb; }

/* Symbol display */
.sym-box { background: #0a0e27; border-radius: 8px; padding: 10px 12px;
           margin-bottom: 12px; font-size: 13px; line-height: 1.8; }
.sym-box span { color: #00d9ff; font-weight: bold; }
.lot-info { color: #ffd700; }

/* Trading buttons */
.btn-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px; }
.btn { padding: 22px 10px; font-size: 17px; font-weight: bold; border: none;
       border-radius: 10px; cursor: pointer; width: 100%; letter-spacing: 0.5px; }
.btn:active { opacity: 0.8; transform: scale(0.97); }
.buy-call  { background: #1b5e20; color: #69f0ae; border: 2px solid #4caf50; }
.buy-put   { background: #b71c1c; color: #ff8a80; border: 2px solid #f44336; }
.exit-call { background: #1a237e; color: #82b1ff; border: 2px solid #3f51b5; }
.exit-put  { background: #4a148c; color: #ea80fc; border: 2px solid #9c27b0; }
.exit-all  { width: 100%; padding: 16px; font-size: 16px; font-weight: bold;
             background: #e65100; color: white; border: none; border-radius: 10px;
             cursor: pointer; margin-bottom: 10px; }
.exit-all:active { opacity: 0.8; }

/* Flash message */
.flash { padding: 10px 14px; border-radius: 8px; margin-bottom: 10px;
         font-size: 13px; text-align: center; font-weight: bold; }
.flash.ok  { background: #1b5e20; color: #69f0ae; }
.flash.err { background: #7b1c1c; color: #ff8a80; }

/* Market status */
.mkt { font-size: 12px; text-align: center; margin-bottom: 10px; color: #aaa; }
.mkt .open   { color: #4caf50; }
.mkt .closed { color: #f44336; }

/* Logs */
.log-box { background: #0a0e27; border-radius: 8px; padding: 10px;
           font-size: 11px; color: #0f0; max-height: 160px; overflow-y: auto;
           font-family: monospace; margin-top: 10px; }
a.back { display: block; text-align: center; margin-top: 14px;
         color: #00d9ff; font-size: 13px; }
</style>
</head>
<body>

<h1>⚡ Manual Trading</h1>

{% if not token %}
<div class="warn-box">❌ Not logged in — <a href="/login" style="color:#ffcdd2">Login first</a></div>
{% else %}
<div class="ok-box">✅ Token Active &nbsp;|&nbsp;
  <span style="color:#ffd700">{{ "DRY RUN" if dry else "LIVE" }}</span>
</div>
{% endif %}

<!-- Instrument Tabs -->
<div class="tabs">
  <a href="/manual?inst=NIFTY"     class="tab {{ 'active' if inst=='NIFTY' }}">NIFTY</a>
  <a href="/manual?inst=BANKNIFTY" class="tab {{ 'active' if inst=='BANKNIFTY' }}">BNIFTY</a>
  <a href="/manual?inst=CRUDEOIL"  class="tab {{ 'active' if inst=='CRUDEOIL' }}">CRUDE</a>
</div>

<!-- Flash message -->
{% if msg %}
<div class="flash {{ 'ok' if msg_ok else 'err' }}">{{ msg }}</div>
{% endif %}

<!-- Market Status -->
<div class="mkt">
  Market:
  {% if mkt_open %}
    <span class="open">🟢 OPEN</span>
  {% else %}
    <span class="closed">🔴 CLOSED ({{ mkt_open_time }} – {{ mkt_close_time }} IST)</span>
  {% endif %}
  &nbsp;|&nbsp; {{ time_now }}
</div>

<!-- Strike Setup -->
<div class="card">
  <h2>⚙️ Strike Setup — {{ inst }}</h2>
  <form method="POST" action="/manual/setup">
    <input type="hidden" name="inst" value="{{ inst }}">
    <div class="row3">
      <div><label>Day</label><input name="day" value="{{ day }}" maxlength="2" placeholder="DD"></div>
      <div><label>Month</label><input name="mon" value="{{ mon }}" maxlength="3" placeholder="AUG"></div>
      <div><label>Year</label><input name="yr"  value="{{ yr  }}" maxlength="2" placeholder="26"></div>
    </div>
    <div class="row2">
      <div><label>CE Strike</label><input name="ce" value="{{ ce_strike }}" placeholder="24500"></div>
      <div><label>PE Strike</label><input name="pe" value="{{ pe_strike }}" placeholder="same as CE"></div>
    </div>
    <div class="row2">
      <div><label>Lots</label><input name="lots" type="number" value="{{ lots }}" min="1"></div>
      <div><label>Qty (auto)</label><input value="{{ qty }} ({{ lots }}×{{ lot_size }})" disabled style="color:#aaa"></div>
    </div>
    <button type="submit" class="save-btn">💾 Save Strikes</button>
  </form>
</div>

<!-- Symbol Display -->
{% if call_sym and put_sym %}
<div class="sym-box">
  📞 CE: <span>{{ call_sym }}</span><br>
  📉 PE: <span>{{ put_sym }}</span><br>
  <span class="lot-info">📦 Qty: {{ qty }} ({{ lots }} lot × {{ lot_size }})</span>
</div>

<!-- Trading Buttons -->
<div class="btn-grid">
  <form method="POST" action="/manual/order">
    <input type="hidden" name="inst" value="{{ inst }}">
    <input type="hidden" name="action" value="BUY_CALL">
    <button type="submit" class="btn buy-call">📈 BUY<br>CALL</button>
  </form>
  <form method="POST" action="/manual/order">
    <input type="hidden" name="inst" value="{{ inst }}">
    <input type="hidden" name="action" value="BUY_PUT">
    <button type="submit" class="btn buy-put">📉 BUY<br>PUT</button>
  </form>
  <form method="POST" action="/manual/order">
    <input type="hidden" name="inst" value="{{ inst }}">
    <input type="hidden" name="action" value="EXIT_CALL">
    <button type="submit" class="btn exit-call">🚪 EXIT<br>CALL</button>
  </form>
  <form method="POST" action="/manual/order">
    <input type="hidden" name="inst" value="{{ inst }}">
    <input type="hidden" name="action" value="EXIT_PUT">
    <button type="submit" class="btn exit-put">🚪 EXIT<br>PUT</button>
  </form>
</div>
<form method="POST" action="/manual/order">
  <input type="hidden" name="inst" value="{{ inst }}">
  <input type="hidden" name="action" value="EXIT_ALL">
  <button type="submit" class="exit-all">⛔ EXIT ALL (CE + PE)</button>
</form>
{% else %}
<div class="warn-box" style="margin-top:10px;">
  ⚠️ Enter strike details above and tap <b>Save Strikes</b> to enable trading buttons.
</div>
{% endif %}

<!-- Recent Logs -->
{% if logs %}
<h2 style="margin-top:14px;">📋 Recent Activity</h2>
<div class="log-box">{% for l in logs %}{{ l }}<br>{% endfor %}</div>
{% endif %}

<a href="/" class="back">← Back to Dashboard</a>

</body>
</html>
"""

@app.route("/manual", methods=["GET"])
def manual():
    inst = request.args.get("inst", "NIFTY").upper()
    if inst not in INSTRUMENTS:
        inst = "NIFTY"

    cfg     = INSTRUMENTS[inst]
    m       = state["manual"][inst]
    now_ist = datetime.now(IST)

    # Parse existing symbol for display
    lots     = m["lots"]
    lot_size = cfg["lot_size"]
    qty      = lots * lot_size

    # Default expiry fields from today
    day = now_ist.strftime("%d")
    mon = now_ist.strftime("%b").upper()
    yr  = now_ist.strftime("%y")

    # Extract strike from saved symbol if available
    ce_strike = ""
    pe_strike = ""
    if m["call"]:
        # e.g. NIFTY11AUG26C24500 → 24500
        match = re.search(r'C(\d+)$', m["call"])
        if match:
            ce_strike = match.group(1)
        # extract expiry day/mon/yr from symbol
        em = re.search(r'(\d{2})([A-Z]{3})(\d{2})', m["call"])
        if em:
            day, mon, yr = em.group(1), em.group(2), em.group(3)
    if m["put"]:
        match = re.search(r'P(\d+)$', m["put"])
        if match:
            pe_strike = match.group(1)

    # Flash message from redirect
    msg    = request.args.get("msg", "")
    msg_ok = request.args.get("ok", "1") == "1"

    recent_logs = [l for l in reversed(state["logs"]) if any(
        k in l for k in ["[ACTION]","[ORDER]","[EXIT]","[OK]","[ERR]","MANUAL"]
    )][:10]

    return render_template_string(MANUAL_HTML,
        inst=inst, token=state["token"], dry=DRY_RUN,
        call_sym=m["call"], put_sym=m["put"],
        qty=qty, lots=lots, lot_size=lot_size,
        day=day, mon=mon, yr=yr,
        ce_strike=ce_strike, pe_strike=pe_strike,
        mkt_open=is_market_open_for(inst),
        mkt_open_time=cfg["market_open"].strftime("%H:%M"),
        mkt_close_time=cfg["market_close"].strftime("%H:%M"),
        time_now=now_ist.strftime("%H:%M:%S"),
        msg=msg, msg_ok=msg_ok,
        logs=recent_logs,
    )

@app.route("/manual/setup", methods=["POST"])
def manual_setup():
    inst = request.form.get("inst", "NIFTY").upper()
    if inst not in INSTRUMENTS:
        inst = "NIFTY"

    cfg       = INSTRUMENTS[inst]
    lot_size  = cfg["lot_size"]
    day       = request.form.get("day", "").strip().zfill(2)
    mon       = request.form.get("mon", "").strip().upper()
    yr        = request.form.get("yr",  "").strip()
    ce        = request.form.get("ce",  "").strip()
    pe        = request.form.get("pe",  "").strip() or ce
    lots      = max(1, int(request.form.get("lots", "1") or "1"))

    if not all([day, mon, yr, ce]):
        return redirect_manual(inst, "❌ Fill all fields!", ok=False)

    expiry   = f"{day}{mon}{yr}"
    call_sym = f"{inst}{expiry}C{ce}"
    put_sym  = f"{inst}{expiry}P{pe}"
    qty      = lots * lot_size

    state["manual"][inst] = {"call": call_sym, "put": put_sym, "qty": qty, "lots": lots}
    add_log(f"[MANUAL] {inst} setup: CE={call_sym} PE={put_sym} QTY={qty}")

    return redirect_manual(inst, f"✅ Saved! CE={call_sym} PE={put_sym} Qty={qty}")

@app.route("/manual/order", methods=["POST"])
def manual_order():
    inst   = request.form.get("inst", "NIFTY").upper()
    action = request.form.get("action", "").upper()

    if inst not in INSTRUMENTS:
        return redirect_manual(inst, "❌ Unknown instrument", ok=False)

    m = state["manual"][inst]
    if not m["call"] or not m["put"]:
        return redirect_manual(inst, "❌ Set strikes first!", ok=False)

    if not state["token"]:
        return redirect_manual(inst, "❌ Not logged in. Visit /login", ok=False)

    ce  = m["call"]
    pe  = m["put"]
    qty = m["qty"]

    add_log(f"[MANUAL] {inst} action={action} CE={ce} PE={pe} QTY={qty}")

    def run():
        if action == "BUY_CALL":
            place_order(ce, "B", qty)
        elif action == "BUY_PUT":
            place_order(pe, "B", qty)
        elif action == "EXIT_CALL":
            exit_position(ce, qty)
        elif action == "EXIT_PUT":
            exit_position(pe, qty)
        elif action == "EXIT_ALL":
            exit_position(ce, qty)
            exit_position(pe, qty)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=12)  # Wait up to 12s for result (faster feedback)

    # Get last log entry for feedback
    last = state["logs"][-1] if state["logs"] else ""
    ok   = "[OK]" in last or "DRY RUN" in last
    return redirect_manual(inst, last.split("] ", 1)[-1] if last else "✅ Order sent", ok=ok)

def redirect_manual(inst, msg, ok=True):
    from flask import redirect as flask_redirect
    from urllib.parse import quote
    return flask_redirect(f"/manual?inst={inst}&msg={quote(msg)}&ok={'1' if ok else '0'}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
