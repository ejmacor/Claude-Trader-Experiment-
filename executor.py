"""
executor.py — Places bracket orders on the Alpaca PAPER account.

All risk guardrails live HERE, outside the model. Claude can only ever
suggest symbols; this file decides whether and how much to buy.
"""

import os

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


def get_account():
    resp = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_open_positions():
    resp = requests.get(f"{BASE_URL}/v2/positions", headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def guardrails_pass(account):
    """Hard checks before ANY new trades. Returns (ok, reason)."""
    equity = float(account["equity"])
    last_equity = float(account["last_equity"])

    if last_equity > 0:
        day_pnl_pct = (equity - last_equity) / last_equity * 100
        if day_pnl_pct <= -config.DAILY_LOSS_HALT_PCT:
            return False, f"Daily loss halt: {day_pnl_pct:.2f}%"

    if len(get_open_positions()) >= config.MAX_OPEN_POSITIONS:
        return False, "Max open positions reached"

    return True, "ok"


def size_position(equity, entry_price):
    """Risk-based sizing: RISK_PER_TRADE_PCT of equity at risk to the stop."""
    risk_dollars = equity * (config.RISK_PER_TRADE_PCT / 100)
    stop_distance = entry_price * (config.STOP_LOSS_PCT / 100)
    shares = int(risk_dollars / stop_distance)

    # Cap by max position size
    max_shares = int(equity * (config.MAX_POSITION_PCT / 100) / entry_price)
    return max(0, min(shares, max_shares))


def place_bracket(symbol, ref_price):
    """Market entry with attached stop-loss and take-profit."""
    account = get_account()
    equity = float(account["equity"])
    qty = size_position(equity, ref_price)

    if qty == 0:
        return {"skipped": True, "reason": "size computed to 0 shares"}

    stop_price = round(ref_price * (1 - config.STOP_LOSS_PCT / 100), 2)
    target_price = round(ref_price * (1 + config.TAKE_PROFIT_PCT / 100), 2)

    order = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "buy",
        "type": "market",
        "time_in_force": config.TIME_IN_FORCE,
        "order_class": "bracket",
        "take_profit": {"limit_price": str(target_price)},
        "stop_loss": {"stop_price": str(stop_price)},
    }

    resp = requests.post(f"{BASE_URL}/v2/orders", headers=HEADERS, json=order, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    return {
        "skipped": False,
        "order_id": result["id"],
        "symbol": symbol,
        "qty": qty,
        "ref_price": ref_price,
        "stop": stop_price,
        "target": target_price,
    }


def flatten_all():
    """Close everything. End-of-day safety net (bracket TIF=day should
    handle it, but belt and suspenders)."""
    resp = requests.delete(
        f"{BASE_URL}/v2/positions",
        headers=HEADERS,
        params={"cancel_orders": "true"},
        timeout=30,
    )
    return resp.status_code in (200, 207)
