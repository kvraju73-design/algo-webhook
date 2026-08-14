"""
TradingView → Flatrade Webhook Bridge
Deploy on Render (Singapore) or Fly.io (Singapore) — FREE tier
Replaces Railway US server. Expected latency: 300–800 ms vs 5–6 sec.

SETUP:
  1. Set these environment variables on your hosting platform:
       FLATRADE_API_KEY   — your Flatrade API key
       FLATRADE_API_SECRET — your Flatrade API secret
       WEBHOOK_SECRET     — any secret string (paste same in TradingView alert URL)
  2. Deploy (see README.md)
  3. In TradingView alert: set webhook URL to
       https://your-app.onrender.com/webhook?secret=YOUR_WEBHOOK_SECRET
"""

import os
import hmac
import logging
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="TV→Flatrade Bridge")

# ── Config from env ──────────────────────────────────────────────
API_KEY      = os.environ.get("FLATRADE_API_KEY", "")
API_SECRET   = os.environ.get("FLATRADE_API_SECRET", "")
WH_SECRET    = os.environ.get("WEBHOOK_SECRET", "changeme")

FLATRADE_BASE = "https://api.flattrade.in"   # update if Flatrade changes this

# ── In-memory token store ───────────────────────────────────────
# Token is pushed daily by running get_token.py on your PC.
# It does NOT auto-login — Flatrade requires browser OAuth each day.
_token_cache: dict = {"token": None}

def _get_token() -> str:
    token = _token_cache.get("token")
    if not token:
        raise RuntimeError(
            "No token set. Run get_token.py on your PC first to login and push today's token."
        )
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
# Set token — called by get_token.py on your PC every morning
# POST /set-token?secret=YOUR_SECRET   body: {"token": "..."}
# ─────────────────────────────────────────────────────────────────
@app.post("/set-token")
async def set_token(request: Request, secret: str = ""):
    if not hmac.compare_digest(secret, WH_SECRET):
        raise HTTPException(status_code=403, detail="Invalid secret")
    body = await request.json()
    token = body.get("token", "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="No token provided")
    _token_cache["token"] = token
    log.info("Token updated via set-token endpoint")
    return {"status": "ok", "message": "Token set successfully"}


# ─────────────────────────────────────────────────────────────────
# Health check (Render pings this to keep the dyno alive)
# ─────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    token_set = bool(_token_cache.get("token"))
    return {"status": "ok", "token_set": token_set}



