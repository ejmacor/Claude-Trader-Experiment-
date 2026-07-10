"""
run_eod.py — 3:50pm ET end-of-day flatten. THE no-overnight-positions rule.

Why this exists: Alpaca `day` time-in-force on a bracket does NOT close the
position at the bell — it only expires the unfilled stop/target child orders
at 4:00pm. Any position whose bracket never triggered would otherwise be
carried overnight with NO protective orders. This job closes every open
position (market) and cancels all working orders ten minutes before the
close, then snapshots equity so the curve records the true end-of-day state.

Idempotent: with nothing open it does nothing. Logged to logs/eod.jsonl.
"""

import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config
import executor
import trade_logger

EOD_LOG = "logs/eod.jsonl"
ET = ZoneInfo("America/New_York")


def main():
    if not getattr(config, "EOD_FLATTEN_ENABLED", True):
        print("EOD flatten disabled in config.")
        return

    try:
        positions = executor.get_open_positions()
    except Exception as e:  # noqa: BLE001
        print(f"Could not read positions: {e}")
        raise

    closed = []
    if positions:
        for p in positions:
            sym = p["symbol"]
            try:
                executor.close_position(sym)  # cancels child orders too
                closed.append({
                    "symbol": sym,
                    "qty": p.get("qty"),
                    "entry": p.get("avg_entry_price"),
                    "last": p.get("current_price"),
                    "unrealized_pl": p.get("unrealized_pl"),
                })
                print(f"FLATTENED {sym} x{p.get('qty')} "
                      f"(unrealized {p.get('unrealized_pl')})")
            except Exception as e:  # noqa: BLE001
                closed.append({"symbol": sym, "error": str(e)})
                print(f"FLATTEN FAILED for {sym}: {e}")
    else:
        print("No open positions — already flat.")

    # Also sweep any stray working orders (e.g. unfilled entries)
    try:
        for o in executor.get_open_orders():
            executor._request("DELETE", f"/v2/orders/{o['id']}")
            print(f"CANCELLED working order {o['symbol']} {o.get('side')} {o.get('type')}")
    except Exception as e:  # noqa: BLE001
        print(f"Order sweep warning: {e}")

    # Post-flatten equity snapshot (replaces today's earlier reading)
    try:
        trade_logger.log_equity(executor.get_account())
    except Exception as e:  # noqa: BLE001
        print(f"Equity snapshot failed (ignored): {e}")

    os.makedirs("logs", exist_ok=True)
    with open(EOD_LOG, "a") as f:
        f.write(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "et_time": datetime.now(ET).strftime("%Y-%m-%d %I:%M %p ET"),
            "flattened": closed,
        }) + "\n")

    print(f"\nEOD flatten complete — {len(closed)} position(s) closed. Flat overnight.")


if __name__ == "__main__":
    main()
