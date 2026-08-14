# =============================================
# ✅ FIX v7: FLATTRADE BLOCKS MKT VIA API — Use aggressive LMT
# Buffer is now configurable + defaults to 3% for guaranteed fills
# =============================================

# Add near top of file (after ORDER_TYPE):
LMT_BUFFER_PCT = float(os.environ.get("LMT_BUFFER_PCT", "3.0"))  # 3% default


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

    # ⚠️ Flattrade API does NOT allow MKT — must use LMT with aggressive buffer
    ltp = get_ltp(symbol, exchange)
    if ltp is None:
        add_log("[ORDER] LTP fetch failed. Aborting.")
        return None

    # Aggressive buffer to simulate market fill
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

            # ⚠️ Flattrade API forces LMT — use aggressive buffer to guarantee fill
            ltp = get_ltp(pos_sym, exchange)
            if ltp is None:
                ltp = float(pos.get("lp", 0)) or float(pos.get("upldprc", 100))
                add_log(f"[EXIT] Using fallback LTP: {ltp}")

            buffer_mult = 1 + (LMT_BUFFER_PCT / 100.0)
            # SELL below market, BUY above market (aggressive for guaranteed fill)
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
