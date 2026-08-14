"""
TradingView → Flatrade Webhook Bridge
Deploy on Render (Singapore) or Fly.io (Singapore) — FREE tier
Replaces Railway US server. Expected latency: 300–800 ms vs 5–6 sec.

SETUP:
  1. Set these environment variables on your hosting platform:
       FLATRADE_API_KEY   — 
       FLATRADE_API_SECRET — 
       WEBHOOK_SECRET     — any secret string (paste same in TradingView alert URL)
  2. Deploy (see README.md)
  3. In TradingView alert: set webhook URL to
       https://your-app.onrender.com/webhook?secret=YOUR_WEBHOOK_SECRET
"""

import os
import time
import hashlib
import hmac
import logging
import httpx
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="TV→Flatrade Bridge")

# ── Config from env ──────────────────────────────────────────────
API_KEY      = os.environ.get(" d531157b153e405aabb13012ffb76cb9", "")
API_SECRET   = os.environ.get("2026.c1d8c2efa998462c88a1e4658bdb3e78b9cb2e0fa98caaca", "")
WH_SECRET    = os.environ.get("WEBHOOK_SECRET", "raju")

FLATRADE_BASE = "https://api.flattrade.in"   # update if Flatrade changes this

# ── In-memory token store (refreshed once per session) ──────────
_token_cache: dict = {"token": None, "expires_at": 0}

# ─────────────────────────────────────────────────────────────────
# Flatrade session token (valid for the trading day)
# ─────────────────────────────────────────────────────────────────
def _get_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    # SHA-256 hash of api_key + api_secret  (Flatrade login method)
    raw = API_KEY + API_SECRET
    sha = hashlib.sha256(raw.encode()).hexdigest()

    resp = httpx.post(
        f"{FLATRADE_BASE}/trade/apitoken",
        json={"api_key": API_KEY, "request_code": sha},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    token = data.get("token") or data.get("susertoken") or data.get("SessionToken")
    if not token:
        raise RuntimeError(f"Token not found in response: {data}")

    _token_cache["token"]      = token
    _token_cache["expires_at"] = now + 6 * 3600   # 6-hour safety window
    log.info("Flatrade token refreshed OK")
    return token


# ─────────────────────────────────────────────────────────────────
# Place order on Flatrade
# ─────────────────────────────────────────────────────────────────
def _place_order(payload: dict) -> dict:
    token = _get_token()

    # Build Flatrade order dict from the TradingView JSON
    # TradingView sends: {"action":"BUY","symbol":"...","qty":65,"type":"MKT","product":"I","exchange":"NFO"}
    action    = payload.get("action", "").upper()       # BUY / SELL
    symbol    = payload.get("symbol", "")
    qty       = int(payload.get("qty", 1))
    order_type = payload.get("type", "MKT")             # MKT / LMT
    product   = payload.get("product", "I")             # I = Intraday
    exchange  = payload.get("exchange", "NFO")

    trans_type = "B" if action == "BUY" else "S"

    order = {
        "uid":      API_KEY,
        "actid":    API_KEY,
        "exch":     exchange,
        "tsym":     symbol,
        "qty":      str(qty),
        "prc":      "0",            # 0 for market orders
        "prd":      product,
        "trantype": trans_type,
        "prctyp":   "MKT" if order_type == "MKT" else "LMT",
        "ret":      "DAY",
    }

    resp = httpx.post(
        f"{FLATRADE_BASE}/trade/placeorder",
        json={"jKey": token, "jData": order},
        timeout=10,
    )
    resp.raise_for_status()
    result = resp.json()
    log.info("Order placed: action=%s symbol=%s qty=%s result=%s", action, symbol, qty, result)
    return result


# ─────────────────────────────────────────────────────────────────
# Webhook endpoint
# TradingView POST → /webhook?secret=YOUR_SECRET
# ─────────────────────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request, secret: str = ""):
    # ── 1. Validate secret ──────────────────────────────────────
    if not hmac.compare_digest(secret, WH_SECRET):
        log.warning("Invalid webhook secret attempt")
        raise HTTPException(status_code=403, detail="Invalid secret")

    # ── 2. Parse body ───────────────────────────────────────────
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    log.info("Webhook received: %s", payload)

    action = payload.get("action", "").upper()
    if action not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    # ── 3. Place order (synchronous — faster than background task) ─
    try:
        result = _place_order(payload)
    except httpx.HTTPStatusError as e:
        log.error("Flatrade HTTP error: %s — %s", e.response.status_code, e.response.text)
        raise HTTPException(status_code=502, detail=f"Flatrade error: {e.response.text}")
    except Exception as e:
        log.error("Order failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({"status": "ok", "action": action, "flatrade": result})


# ─────────────────────────────────────────────────────────────────
# Health check (Render pings this to keep the dyno alive)
# ─────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "token_cached": bool(_token_cache["token"])}


# ─────────────────────────────────────────────────────────────────
# Force token refresh (call this at 9:00 AM IST before market opens)
# GET /refresh-token?secret=YOUR_SECRET
# ─────────────────────────────────────────────────────────────────
@app.get("/refresh-token")
def refresh_token(secret: str = ""):
    if not hmac.compare_digest(secret, WH_SECRET):
        raise HTTPException(status_code=403, detail="Invalid secret")
    _token_cache["token"]      = None
    _token_cache["expires_at"] = 0
    try:
        _get_token()
        return {"status": "ok", "message": "Token refreshed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
