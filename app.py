"""
Flattrade NIFTY CE/PE - CLOUD VERSION for Railway
==================================================
- Receives TradingView webhooks
- Web-based daily login (/login)
- Web-based strike setup (/setup)
- Uses AlgoIP proxy for whitelisted IP
- No terminal input needed (cloud-safe)
"""

import os
import json
import hashlib
import logging
import sys
import threading
import time
import requests
from datetime import datetime, time as dtime
from flask import Flask, request, jsonify, render_template_string, redirect, url_for
import pytz

# =============================================
# CONFIG - Loaded from Railway Environment Variables
# =============================================
USER_ID        = os.environ.get("USER_ID", "")
API_KEY        = os.environ.get("API_KEY", "")
API_SECRET     = os.environ.get("API_SECRET", "")

PROXY_HOST     = os.environ.get("PROXY_HOST", "")
PROXY_PORT     = os.environ.get("PROXY_PORT", "443")
PROXY_USER     = os.environ.get("PROXY_USER", "")
PROXY_PASS     = os.environ.get("PROXY_PASS", "")

EXCHANGE       = os.environ.get("EXCHANGE", "NFO")
PRODUCT        = os.environ.get("PRODUCT", "M")
LOT_SIZE       = int(os.environ.get("LOT_SIZE", "65"))
DRY_RUN        = os.environ.get("DRY_RUN", "False").lower() == "true"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# =============================================
PROXIES = {
    "http":  f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}",
    "https": f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}",
} if PROXY_HOST else None

TOKEN_URL      = "https://authapi.flattrade.in/trade/apitoken"
BASE_URL_ORDER = "https://piconnect.flattrade.in/PiConnectAPI"

# -----------------------------------------------
# LOGGING
# -----------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# -----------------------------------------------
# STATE (in-memory - reset on redeploy)
# -----------------------------------------------
state = {
    "token":       None,
    "call_symbol": "",
    "put_symbol":  "",
    "qty":         LOT_SIZE,
    "logs":        [],  # keep last 100 log entries
}

IST = pytz.timezone("Asia/Kolkata")

def add_log(msg):
    ts = datetime.now(IST).strftime("%H:%M:%S")
    state["logs"].append(f"[{ts}] {msg}")
    state["logs"] = state["logs"][-100:]  # keep last 100
    log.info(msg)

# -----------------------------------------------
# MARKET HOURS
# -----------------------------------------------
def is_market_open():
    now = datetime.now(IST).time()
    return dtime(9, 15) <= now <= dtime(15, 30)

# -----------------------------------------------
# TOKEN GENERATION
# -----------------------------------------------
def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()

def exchange_request_code(request_code):
    """Called by /login POST form"""
    hash_value = sha256(API_KEY + request_code + API_SECRET)
    payload = {
        "api_key":      API_KEY,
        "request_code": request_code,
        "api_secret":   hash_value
    }
    try:
        r = requests.post(TOKEN_URL, json=payload, proxies=PROXIES, timeout=15)
        resp = r.json()
    except Exception as e:
        add_log(f"[LOGIN] Token request failed: {e}")
        return False, str(e)

    if resp.get("stat") != "Ok":
        err = resp.get("emsg", str(resp))
        add_log(f"[LOGIN] Token generation failed: {err}")
        return False, err

    token = resp.get("token") or resp.get("susertoken")
    if not token:
        add_log(f"[LOGIN] No token in response: {resp}")
        return False, "No token returned"

    state["token"] = token
    add_log("[LOGIN] Token generated successfully.")
    return True, "OK"

# -----------------------------------------------
# FLATTRADE API CALLS
# -----------------------------------------------
def get_token_for_symbol(symbol):
    url = BASE_URL_ORDER + "/SearchScrip"
    payload = {"uid": USER_ID, "stext": symbol, "exch": EXCHANGE}
    try:
        r = requests.post(
            url,
            data="jData=" + json.dumps(payload) + "&jKey=" + state["token"],
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            proxies=PROXIES, timeout=8
        )
        resp = r.json()
        if resp.get("stat") == "Ok":
            values = resp.get("values", [])
            for v in values:
                if v.get("tsym") == symbol:
                    return v.get("token")
            if values:
                return values[0].get("token")
        add_log(f"[LOOKUP] Failed for {symbol}: {resp.get('emsg')}")
    except Exception as e:
        add_log(f"[LOOKUP] Exception: {e}")
    return None

def get_ltp(symbol):
    numeric_token = get_token_for_symbol(symbol)
    if not numeric_token:
        return None
    url = BASE_URL_ORDER + "/GetQuotes"
    payload = {"uid": USER_ID, "exch": EXCHANGE, "token": numeric_token}
    try:
        r = requests.post(
            url,
            data="jData=" + json.dumps(payload) + "&jKey=" + state["token"],
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

def place_order(symbol, trantype, qty=None):
    action = "BUY" if trantype == "B" else "SELL"
    qty = qty or state["qty"]
    add_log(f"[ORDER] {action} → {symbol} | qty={qty}")

    if not state["token"]:
        add_log("[ORDER] ERROR: No token. Visit /login first!")
        return None

    if not DRY_RUN and not is_market_open():
        add_log("[ORDER] Market closed (09:15–15:30 IST)")
        return None

    ltp = get_ltp(symbol)
    if ltp is None:
        add_log("[ORDER] LTP fetch failed. Aborting.")
        return None

    price = round(ltp * 1.005, 1) if trantype == "B" else round(ltp * 0.995, 1)

    if DRY_RUN:
        add_log(f"[DRY RUN] {action} {qty} {symbol} @ {price} (LTP {ltp})")
        return "DRY_RUN"

    url = BASE_URL_ORDER + "/PlaceOrder"
    payload = {
        "uid": USER_ID, "actid": USER_ID,
        "exch": EXCHANGE, "tsym": symbol,
        "qty": str(qty), "prc": str(price),
        "prd": PRODUCT, "trantype": trantype,
        "prctyp": "LMT", "ret": "DAY"
    }
    try:
        r = requests.post(
            url,
            data="jData=" + json.dumps(payload) + "&jKey=" + state["token"],
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            proxies=PROXIES, timeout=8
        )
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

    if not state["token"]:
        add_log("[EXIT] ERROR: No token. Visit /login first!")
        return

    if not DRY_RUN and not is_market_open():
        add_log("[EXIT] Market closed")
        return

    if DRY_RUN:
        add_log(f"[DRY RUN] Would exit {symbol}")
        return

    url = BASE_URL_ORDER + "/PositionBook"
    payload = {"uid": USER_ID, "actid": USER_ID}
    try:
        r = requests.post(
            url,
            data="jData=" + json.dumps(payload) + "&jKey=" + state["token"],
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            proxies=PROXIES, timeout=8
        )
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
            ltp = get_ltp(symbol) or float(pos.get("lp", 100))
            price = round(ltp * 0.995, 1) if trantype == "S" else round(ltp * 1.005, 1)

            sq_payload = {
                "uid": USER_ID, "actid": USER_ID,
                "exch": EXCHANGE, "tsym": symbol,
                "qty": str(exit_qty), "prc": str(price),
                "prd": PRODUCT, "trantype": trantype,
                "prctyp": "LMT", "ret": "DAY"
            }
            sq_r = requests.post(
                BASE_URL_ORDER + "/PlaceOrder",
                data="jData=" + json.dumps(sq_payload) + "&jKey=" + state["token"],
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                proxies=PROXIES, timeout=8
            )
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
def handle_action(action, qty=None):
    qty = qty or state["qty"]
    action = action.upper().strip()
    ce = state["call_symbol"]
    pe = state["put_symbol"]

    if not ce or not pe:
        add_log(f"[ACTION] ERROR: Strikes not set! Visit /setup first.")
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

# ---------- HOME ----------
HOME_HTML = """
<!DOCTYPE html>
<html><head><title>Flattrade Algo</title>
<style>
body{font-family:Arial;background:#1a1a2e;color:#eee;padding:30px;max-width:800px;margin:auto;}
h1{color:#00d9ff;}h2{color:#ffd700;margin-top:30px;}
.status{padding:15px;background:#16213e;border-radius:8px;margin:10px 0;}
.ok{color:#4caf50;}.err{color:#f44336;}.warn{color:#ff9800;}
a{color:#00d9ff;text-decoration:none;padding:10px 20px;background:#0f3460;
  border-radius:5px;display:inline-block;margin:5px;}
a:hover{background:#e94560;}
</style></head><body>
<h1>🚀 Flattrade Algo — Cloud</h1>
<div class="status">
<b>Token:</b> <span class="{{'ok' if token else 'err'}}">{{'✅ Active' if token else '❌ Not logged in'}}</span><br>
<b>CE Symbol:</b> {{ce or '<span class="warn">Not set</span>' | safe}}<br>
<b>PE Symbol:</b> {{pe or '<span class="warn">Not set</span>' | safe}}<br>
<b>Qty/Order:</b> {{qty}}<br>
<b>Mode:</b> <span class="{{'warn' if dry else 'ok'}}">{{'DRY RUN' if dry else 'LIVE'}}</span><br>
<b>Market:</b> <span class="{{'ok' if mkt else 'warn'}}">{{'OPEN' if mkt else 'CLOSED'}}</span>
</div>
<h2>Quick Actions</h2>
<a href="/login">🔑 Daily Login</a>
<a href="/setup">⚙️ Strike Setup</a>
<a href="/logs">📋 View Logs</a>
<a href="/health">💚 Health</a>
<h2>Webhook URL for TradingView</h2>
<div class="status"><code>{{webhook_url}}</code></div>
</body></html>
"""

@app.route("/")
def home():
    return render_template_string(
        HOME_HTML,
        token=state["token"],
        ce=state["call_symbol"],
        pe=state["put_symbol"],
        qty=state["qty"],
        dry=DRY_RUN,
        mkt=is_market_open(),
        webhook_url=request.url_root + "webhook"
    )

# ---------- LOGIN ----------
LOGIN_HTML = """
<!DOCTYPE html>
<html><head><title>Flattrade Login</title>
<style>
body{font-family:Arial;background:#1a1a2e;color:#eee;padding:30px;max-width:700px;margin:auto;}
h1{color:#00d9ff;}
.box{background:#16213e;padding:20px;border-radius:8px;margin:15px 0;}
a.btn,button{background:#e94560;color:white;padding:12px 25px;border:none;
  border-radius:5px;text-decoration:none;font-size:16px;cursor:pointer;display:inline-block;}
input{width:100%;padding:10px;font-size:16px;background:#0f3460;color:white;
  border:1px solid #00d9ff;border-radius:5px;margin:10px 0;box-sizing:border-box;}
.step{margin:20px 0;padding:15px;background:#0f3460;border-left:4px solid #00d9ff;}
.msg{padding:15px;border-radius:5px;margin:15px 0;}
.ok{background:#1b5e20;}.err{background:#b71c1c;}
</style></head><body>
<h1>🔑 Daily Flattrade Login</h1>
{% if msg %}<div class="msg {{'ok' if success else 'err'}}">{{msg}}</div>{% endif %}
<div class="step">
<b>Step 1:</b> Click below to open Flattrade login in new tab<br><br>
<a class="btn" href="https://auth.flattrade.in/?app_key={{api_key}}" target="_blank">🚀 Login to Flattrade</a>
</div>
<div class="step">
<b>Step 2:</b> After login, browser redirects to a URL like:<br>
<code>http://127.0.0.1?request_code=XXXXXXXXXXXX</code><br><br>
<b>Copy the request_code value from that URL</b> and paste below:
</div>
<form method="POST">
<input name="request_code" placeholder="Paste request_code here" required autofocus>
<button type="submit">Generate Token</button>
</form>
<br><a href="/">← Back to Home</a>
</body></html>
"""

@app.route("/login", methods=["GET", "POST"])
def login():
    msg = None
    success = False
    if request.method == "POST":
        code = request.form.get("request_code", "").strip()
        if code:
            success, result = exchange_request_code(code)
            msg = "✅ Token generated! You can close this page." if success else f"❌ {result}"
    return render_template_string(LOGIN_HTML, msg=msg, success=success, api_key=API_KEY)

# ---------- SETUP ----------
SETUP_HTML = """
<!DOCTYPE html>
<html><head><title>Strike Setup</title>
<style>
body{font-family:Arial;background:#1a1a2e;color:#eee;padding:30px;max-width:600px;margin:auto;}
h1{color:#00d9ff;}
.box{background:#16213e;padding:20px;border-radius:8px;}
label{display:block;margin:15px 0 5px;color:#ffd700;}
input,select{width:100%;padding:10px;font-size:16px;background:#0f3460;color:white;
  border:1px solid #00d9ff;border-radius:5px;box-sizing:border-box;}
button{background:#e94560;color:white;padding:12px 25px;border:none;
  border-radius:5px;font-size:16px;cursor:pointer;margin-top:20px;}
.ok{background:#1b5e20;padding:15px;border-radius:5px;margin:15px 0;}
</style></head><body>
<h1>⚙️ Strike Setup</h1>
{% if saved %}<div class="ok">✅ Saved!<br>CE: {{ce}}<br>PE: {{pe}}<br>Qty: {{qty}}</div>{% endif %}
<div class="box">
<form method="POST">
<label>Day (DD):</label><input name="day" value="{{day}}" required>
<label>Month (JAN..DEC):</label><input name="mon" value="{{mon}}" required>
<label>Year (YY):</label><input name="yr" value="{{yr}}" required>
<label>CE Strike:</label><input name="ce_strike" value="{{ce_strike}}" required>
<label>PE Strike (blank = same as CE):</label><input name="pe_strike" value="{{pe_strike}}">
<label>Lots:</label><input name="lots" type="number" value="{{lots}}" required>
<button type="submit">Save Strikes</button>
</form>
</div>
<br><a href="/" style="color:#00d9ff;">← Back to Home</a>
</body></html>
"""

@app.route("/setup", methods=["GET", "POST"])
def setup():
    now_ist = datetime.now(IST)
    defaults = {
        "day": now_ist.strftime("%d"),
        "mon": now_ist.strftime("%b").upper(),
        "yr":  now_ist.strftime("%y"),
        "ce_strike": "24500",
        "pe_strike": "",
        "lots": "1",
    }
    saved = False

    if request.method == "POST":
        day = request.form.get("day", "").strip().zfill(2)
        mon = request.form.get("mon", "").strip().upper()
        yr  = request.form.get("yr", "").strip()
        ce_strike = request.form.get("ce_strike", "").strip()
        pe_strike = request.form.get("pe_strike", "").strip() or ce_strike
        lots = int(request.form.get("lots", "1"))

        expiry = f"{day}{mon}{yr}"
        state["call_symbol"] = f"NIFTY{expiry}C{ce_strike}"
        state["put_symbol"]  = f"NIFTY{expiry}P{pe_strike}"
        state["qty"] = lots * LOT_SIZE
        add_log(f"[SETUP] CE={state['call_symbol']} PE={state['put_symbol']} QTY={state['qty']}")
        saved = True
        defaults.update({"day": day, "mon": mon, "yr": yr,
                         "ce_strike": ce_strike, "pe_strike": pe_strike, "lots": lots})

    return render_template_string(SETUP_HTML, saved=saved,
                                  ce=state["call_symbol"], pe=state["put_symbol"],
                                  qty=state["qty"], **defaults)

# ---------- LOGS ----------
@app.route("/logs")
def logs():
    lines = "\n".join(state["logs"][::-1])  # newest first
    return f"<pre style='background:#000;color:#0f0;padding:20px;font-size:13px;'>{lines}</pre>"

# ---------- WEBHOOK ----------
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
        qty_raw = data.get("qty", str(state["qty"]))
        try:
            qty = int(str(qty_raw).split(".")[0])
        except Exception:
            qty = state["qty"]

        t = threading.Thread(target=handle_action, args=(action, qty), daemon=True)
        t.start()

        return jsonify({"status": "ok", "action": action, "qty": qty}), 200
    except Exception as e:
        add_log(f"[WEBHOOK] Exception: {e}")
        return jsonify({"status": "error", "msg": str(e)}), 500

# ---------- HEALTH ----------
@app.route("/health")
def health():
    return jsonify({
        "status":   "running",
        "dry_run":  DRY_RUN,
        "ce":       state["call_symbol"],
        "pe":       state["put_symbol"],
        "qty":      state["qty"],
        "token_ok": bool(state["token"]),
        "market_open": is_market_open(),
        "time_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    }), 200

# -----------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)