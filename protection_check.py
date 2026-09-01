"""
protection_check.py — no position sits without a live stop.

THE BUG THIS CLOSES
-------------------
DAY_MOMENTUM brackets are submitted with time_in_force="day". At 4:00pm ET
Alpaca expires the unfilled stop/target child orders but does NOT close the
position. If the EOD flatten job fails to run, the position survives the
night with no protective order attached.

Two things then go wrong, and the second one is the killer:

  1. The position is genuinely unprotected overnight (the AEHR incident).
  2. executor.current_portfolio_heat_pct() cannot find a stop order, so it
     charges that position the conservative STOP_PCT_CEIL (8%) instead of
     its real entry->stop distance (~3%). Inflated heat trips the
     MAX_PORTFOLIO_HEAT_PCT guardrail, the morning run exits before the
     scanner, and nothing can ever close the position that caused it.
     Deadlock. That is what froze the account from 2026-08-12 onward.

Re-arming the real stop fixes both: the position is protected AND the heat
calculation reports the truth, so the guardrail stops firing on a phantom.

This module NEVER changes a risk parameter. It restores the stop that was
already logged at entry in logs/trade_log.csv.
"""

import argparse
import csv
import json
import os
import time
from datetime import datetime, timezone

import config
import executor

PROTECTION_LOG = "logs/protection.jsonl"
TRADE_LOG = "logs/trade_log.csv"


def logged_stop_for(symbol):
    """The stop price recorded when this position was opened.

    Uses the most recent non-skipped entry row for the symbol. Returns None
    if the trade log has nothing usable.
    """
    if not os.path.exists(TRADE_LOG):
        return None
    stop = None
    try:
        with open(TRADE_LOG, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("symbol") != symbol:
                    continue
                if str(row.get("skipped", "")).lower() == "true":
                    continue
                try:
                    val = float(row.get("stop") or 0)
                except (TypeError, ValueError):
                    continue
                if val > 0:
                    stop = val  # later rows win
    except Exception as e:  # noqa: BLE001
        print(f"[protection] could not read {TRADE_LOG}: {e}")
    return stop


def live_sell_stops():
    """symbol -> stop_price for every working protective sell order."""
    stops = {}
    for o in executor.get_open_orders():
        if o.get("side") == "sell" and o.get("type") in ("stop", "stop_limit"):
            try:
                stops[o["symbol"]] = float(o.get("stop_price") or 0)
            except (TypeError, ValueError):
                continue
    return stops


def cancel_sell_orders(symbol):
    """Clear working sell orders on a symbol.

    Why this is separate: close_position() with cancel_orders=true has raced
    against its own child orders before, leaving a cancelled sell and an
    un-closed position. Cancelling first, then closing, is deterministic.
    """
    cancelled = []
    for o in executor.get_open_orders():
        if o.get("symbol") == symbol and o.get("side") == "sell":
            try:
                executor._request("DELETE", f"/v2/orders/{o['id']}")
                cancelled.append(o["id"])
            except Exception as e:  # noqa: BLE001
                print(f"[protection] cancel failed {symbol} {o['id']}: {e}")
    if cancelled:
        time.sleep(1)
    return cancelled


def arm_stop(symbol, qty, stop_price):
    """Place a standalone GTC sell stop. GTC deliberately: a `day` stop is
    exactly how these positions became unprotected in the first place."""
    order = {
        "symbol": symbol,
        "qty": str(int(float(qty))),
        "side": "sell",
        "type": "stop",
        "stop_price": str(round(float(stop_price), 2)),
        "time_in_force": "gtc",
    }
    return executor._request("POST", "/v2/orders", json=order).json()


def check(dry_run=False):
    """Audit every open position. Returns a list of action records.

    Outcomes per position:
      protected      — a live stop already exists, nothing done
      rearmed        — stop was missing, the logged stop was restored
      beyond_stop    — price is already through the stop; flagged for close
                       (this module does not sell; preflight/EOD handles it)
      unresolved     — no stop could be determined from the trade log
    """
    actions = []
    try:
        positions = executor.get_open_positions()
    except Exception as e:  # noqa: BLE001
        print(f"[protection] could not read positions: {e}")
        return [{"error": str(e)}]

    if not positions:
        print("[protection] no open positions — nothing to protect.")
        return actions

    stops = live_sell_stops()

    for p in positions:
        sym = p["symbol"]
        qty = p.get("qty")
        try:
            last = float(p.get("current_price") or 0)
            entry = float(p.get("avg_entry_price") or 0)
        except (TypeError, ValueError):
            last, entry = 0.0, 0.0

        existing = stops.get(sym)
        if existing and existing > 0:
            actions.append({"symbol": sym, "action": "protected", "stop": existing})
            print(f"[protection] {sym:6s} OK — live stop at {existing}")
            continue

        stop = logged_stop_for(sym)
        if not stop and entry:
            # Last resort: the ceiling the heat calc already assumes.
            stop = round(entry * (1 - config.STOP_PCT_CEIL / 100), 2)
            source = f"fallback {config.STOP_PCT_CEIL}% of entry"
        else:
            source = "logged entry stop"

        if not stop:
            actions.append({"symbol": sym, "action": "unresolved",
                            "detail": "no stop in trade log and no entry price"})
            print(f"[protection] {sym:6s} UNRESOLVED — no stop could be determined")
            continue

        if last and last <= stop:
            actions.append({"symbol": sym, "action": "beyond_stop",
                            "stop": stop, "last": last,
                            "detail": "price already through the stop — needs closing, not arming"})
            print(f"[protection] {sym:6s} BEYOND STOP — last {last} <= stop {stop}; flagged for close")
            continue

        if dry_run:
            actions.append({"symbol": sym, "action": "would_rearm", "stop": stop, "source": source})
            print(f"[protection] {sym:6s} would re-arm stop at {stop} ({source})")
            continue

        try:
            cancel_sell_orders(sym)
            resp = arm_stop(sym, qty, stop)
            actions.append({"symbol": sym, "action": "rearmed", "stop": stop,
                            "source": source, "order_id": resp.get("id")})
            print(f"[protection] {sym:6s} RE-ARMED stop at {stop} ({source})")
        except Exception as e:  # noqa: BLE001
            actions.append({"symbol": sym, "action": "rearm_failed", "stop": stop, "error": str(e)})
            print(f"[protection] {sym:6s} RE-ARM FAILED: {e}")

    if actions and not dry_run:
        os.makedirs("logs", exist_ok=True)
        with open(PROTECTION_LOG, "a") as f:
            f.write(json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "actions": actions,
            }) + "\n")
    return actions


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Audit and restore protective stops.")
    ap.add_argument("--dry-run", action="store_true", help="report only, place nothing")
    args = ap.parse_args()
    result = check(dry_run=args.dry_run)
    print()
    print(json.dumps(result, indent=2))
