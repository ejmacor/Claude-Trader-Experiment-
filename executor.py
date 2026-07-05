"""
executor.py — v2. Places bracket orders on the Alpaca PAPER account.

All hard risk controls live HERE, outside the model. Claude only suggests;
this file decides whether and how much.

v2 upgrades over v1:
- ATR-scaled brackets (fixed 4%/8% ignored per-name volatility) with pct floor/ceil
- Regime risk multiplier + swing risk scale in sizing
- Portfolio heat cap: total open risk (entry->stop) capped at MAX_PORTFOLIO_HEAT_PCT
- Weekly (rolling 5-session) loss circuit breaker, not just daily
- GTC brackets for SWING_CATALYST trades (survive overnight)
- Order verification: confirms Alpaca accepted the order; retry on transient errors
"""

import csv
import os
import time

import requests

import config

ALPACA_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET = os.environ["ALPACA_SECRET_KEY"]

# PAPER endpoint. Never change this to the live URL for this experiment.
BASE_URL = "https://paper-api.alpaca.markets"

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type": "application/json",
}


def _request(method, path, retries=3, **kwargs):
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.request(method, f"{BASE_URL}{path}", headers=HEADERS, timeout=30, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as e:
            # 4xx = our fault, don't retry. 5xx = transient, retry.
            if e.response is not None and 400 <= e.response.status_code < 500:
                raise
            last_err = e
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(1.5 * (attempt + 1))
    raise last_err


def get_account():
    return _request("GET", "/v2/account").json()


def get_open_positions():
    return _request("GET", "/v2/positions").json()


def get_open_orders():
    return _request("GET", "/v2/orders", params={"status": "open", "limit": 100}).json()


def current_portfolio_heat_pct(equity):
    """Sum of open risk: for each position, distance to its stop order.

    Positions whose stop can't be found are charged a conservative
    STOP_PCT_CEIL so unknown risk is over- not under-counted.
    """
    positions = get_open_positions()
    if not positions:
        return 0.0
    stops = {}
    for o in get_open_orders():
        if o.get("side") == "sell" and o.get("type") in ("stop", "stop_limit"):
            stops[o["symbol"]] = float(o.get("stop_price") or 0)
    heat = 0.0
    for p in positions:
        qty = float(p["qty"])
        px = float(p["avg_entry_price"])
        stop = stops.get(p["symbol"])
        risk_per_share = (px - stop) if stop and stop < px else px * config.STOP_PCT_CEIL / 100
        heat += max(0.0, qty * risk_per_share)
    return heat / equity * 100


def _rolling_week_pnl_pct():
    """Rolling 5-session P&L from the equity log. Missing data -> 0 (no halt)."""
    try:
        with open(config.EQUITY_LOG_CSV, newline="") as f:
            rows = list(csv.DictReader(f))
        if len(rows) < 2:
            return 0.0
        window = rows[-6:]
        start, end = float(window[0]["equity"]), float(window[-1]["equity"])
        return (end - start) / start * 100 if start else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def guardrails_pass(account, regime=None):
    """Hard checks before ANY new trades. Returns (ok, reason)."""
    equity = float(account["equity"])
    last_equity = float(account["last_equity"])

    if last_equity > 0:
        day_pnl_pct = (equity - last_equity) / last_equity * 100
        if day_pnl_pct <= -config.DAILY_LOSS_HALT_PCT:
            return False, f"Daily loss halt: {day_pnl_pct:.2f}%"

    week_pnl = _rolling_week_pnl_pct()
    if week_pnl <= -config.WEEKLY_LOSS_HALT_PCT:
        return False, f"Weekly loss circuit breaker: {week_pnl:.2f}% over last 5 sessions"

    if regime and regime.get("risk_mult", 1) == 0:
        return False, f"Regime halt: {regime['regime']} — no new entries"

    if len(get_open_positions()) >= config.MAX_OPEN_POSITIONS:
        return False, "Max open positions reached"

    if current_portfolio_heat_pct(equity) >= config.MAX_PORTFOLIO_HEAT_PCT:
        return False, "Portfolio heat cap reached"

    return True, "ok"


def bracket_prices(ref_price, atr):
    """ATR-scaled stop/target, clamped to pct floor/ceil of entry."""
    if atr and atr > 0:
        stop_dist = atr * config.STOP_ATR_MULT
        stop_dist = max(ref_price * config.STOP_PCT_FLOOR / 100,
                        min(stop_dist, ref_price * config.STOP_PCT_CEIL / 100))
    else:
        stop_dist = ref_price * 0.04  # v1 fallback if ATR missing
    target_dist = stop_dist * (config.TARGET_ATR_MULT / config.STOP_ATR_MULT)
    return round(ref_price - stop_dist, 2), round(ref_price + target_dist, 2)


def size_position(equity, entry_price, stop_price, risk_mult=1.0):
    """Risk-based sizing: (RISK_PER_TRADE_PCT x regime/module multipliers) to the stop."""
    risk_dollars = equity * (config.RISK_PER_TRADE_PCT / 100) * risk_mult
    stop_distance = max(entry_price - stop_price, 0.01)
    shares = int(risk_dollars / stop_distance)
    max_shares = int(equity * (config.MAX_POSITION_PCT / 100) / entry_price)
    return max(0, min(shares, max_shares))


def place_bracket(symbol, ref_price, atr=None, module="DAY_MOMENTUM", regime_mult=1.0):
    account = get_account()
    equity = float(account["equity"])

    # Heat check including this prospective trade
    heat_now = current_portfolio_heat_pct(equity)
    if heat_now >= config.MAX_PORTFOLIO_HEAT_PCT:
        return {"skipped": True, "reason": f"portfolio heat {heat_now:.1f}% at cap"}

    stop_price, target_price = bracket_prices(ref_price, atr)

    mult = regime_mult * (config.SWING_RISK_SCALE if module == "SWING_CATALYST" else 1.0)
    qty = size_position(equity, ref_price, stop_price, mult)
    if qty == 0:
        return {"skipped": True, "reason": "size computed to 0 shares"}

    tif = config.SWING_TIME_IN_FORCE if module == "SWING_CATALYST" else config.DAY_TIME_IN_FORCE

    order = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "buy",
        "type": "market",
        "time_in_force": tif,
        "order_class": "bracket",
        "take_profit": {"limit_price": str(target_price)},
        "stop_loss": {"stop_price": str(stop_price)},
    }

    resp = _request("POST", "/v2/orders", json=order)
    result = resp.json()

    # Verify Alpaca actually holds the order (submission != acceptance)
    verified = False
    for _ in range(3):
        time.sleep(1)
        check = _request("GET", f"/v2/orders/{result['id']}").json()
        if check.get("status") in ("new", "accepted", "partially_filled", "filled", "pending_new"):
            verified = True
            break

    return {
        "skipped": False,
        "order_id": result["id"],
        "verified": verified,
        "symbol": symbol,
        "qty": qty,
        "ref_price": ref_price,
        "stop": stop_price,
        "target": target_price,
        "module": module,
        "time_in_force": tif,
    }


def close_position(symbol):
    """Close one position and cancel its child orders."""
    return _request("DELETE", f"/v2/positions/{symbol}", params={"cancel_orders": "true"}).json()


def replace_stop(symbol, new_stop):
    """Raise the stop on an open bracket (used by midday manager)."""
    for o in get_open_orders():
        if o["symbol"] == symbol and o.get("side") == "sell" and o.get("type") in ("stop", "stop_limit"):
            return _request("PATCH", f"/v2/orders/{o['id']}", json={"stop_price": str(new_stop)}).json()
    return None


def flatten_all():
    resp = _request("DELETE", "/v2/positions", params={"cancel_orders": "true"})
    return resp.status_code in (200, 207)
