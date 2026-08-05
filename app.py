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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)