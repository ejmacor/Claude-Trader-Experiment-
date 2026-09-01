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
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config
import executor
import health
import protection_check
import trade_logger

EOD_LOG = "logs/eod.jsonl"
ET = ZoneInfo("America/New_York")


def main():
    if not getattr(config, "EOD_FLATTEN_ENABLED", True):
        print("EOD flatten disabled in config.")
        health.mark("eod", "SKIPPED", "EOD_FLATTEN_ENABLED is False in config")
        return

    try:
        positions = executor.get_open_positions()
    except Exception as e:  # noqa: BLE001
        print(f"Could not read positions: {e}")
        health.mark("eod", "ERROR", f"could not read positions: {e}")
        raise

    closed = []
    if positions:
        for p in positions:
            sym = p["symbol"]
            try:
                # Cancel working sells first, THEN close. close_position with
                # cancel_orders=true has raced against its own child orders
                # before, leaving a cancelled stop and a live position.
                protection_check.cancel_sell_orders(sym)
                executor.close_position(sym)
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

    # Verify, then escalate. "I sent the close" is not "the position is gone",
    # and an unnoticed failure here is what stranded three positions for weeks.
    failures = [c for c in closed if c.get("error")]
    leftover = []
    try:
        leftover = executor.get_open_positions()
    except Exception as e:  # noqa: BLE001
        print(f"Post-flatten verification failed: {e}")

    if leftover:
        print(f"STILL OPEN after per-symbol close: {[p['symbol'] for p in leftover]} "
              f"— escalating to flatten_all()")
        try:
            executor.flatten_all()
            time.sleep(2)
            leftover = executor.get_open_positions()
        except Exception as e:  # noqa: BLE001
            print(f"flatten_all() failed: {e}")

    if leftover:
        # Last line of defence: if we cannot close it, at least protect it,
        # so tomorrow's heat calculation sees a real stop instead of the 8%
        # ceiling and the morning run is not locked out.
        print("Could not flatten everything — arming protective stops instead.")
        try:
            protection_check.check()
        except Exception as e:  # noqa: BLE001
            print(f"Protective re-arm failed: {e}")
        health.mark("eod", "ERROR",
                    f"{len(leftover)} position(s) still open after flatten: "
                    f"{[p['symbol'] for p in leftover]}")
    elif failures:
        health.mark("eod", "ERROR", f"{len(failures)} close error(s), account now flat")
    else:
        health.mark("eod", "OK", f"{len(closed)} position(s) closed, account flat")

    print(f"\nEOD flatten complete — {len(closed)} position(s) closed, "
          f"{len(leftover)} still open.")


if __name__ == "__main__":
    main()
