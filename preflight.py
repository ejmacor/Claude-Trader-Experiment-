"""
preflight.py — remediation that runs BEFORE the risk guardrails.

ORDERING IS THE FIX
-------------------
run_morning.py used to check guardrails first and exit on failure. That made
the halt self-sealing: stale, unprotected positions inflated portfolio heat,
heat tripped the guardrail, the guardrail exited before any code that could
have cleaned up the positions. Twenty-one trading days of nothing.

Remediation now runs first. Guardrails still have absolute veto over NEW
trades — no risk parameter is relaxed anywhere in this file — but the system
is always allowed to clean up after itself before it judges itself.

Two jobs:
  1. Re-arm protective stops (protection_check) so heat reports reality
     instead of the 8% ceiling fallback.
  2. Close positions that outlived their intended hold. A DAY_MOMENTUM
     position opened on an earlier date should not exist; the EOD flatten
     is supposed to guarantee that, and when it fails this is the backstop.

--sweep mode is for the intraday workflow, when the market is definitely
open and market orders will actually fill.
"""

import argparse
import csv
import json
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config
import executor
import health
import protection_check

ET = ZoneInfo("America/New_York")
PREFLIGHT_LOG = "logs/preflight.jsonl"
TRADE_LOG = "logs/trade_log.csv"


def _today_et():
    return datetime.now(ET).date().isoformat()


def entry_rows_by_symbol():
    """symbol -> most recent non-skipped trade_log row."""
    rows = {}
    if not os.path.exists(TRADE_LOG):
        return rows
    try:
        with open(TRADE_LOG, newline="") as f:
            for r in csv.DictReader(f):
                if str(r.get("skipped", "")).lower() == "true":
                    continue
                if not r.get("symbol"):
                    continue
                rows[r["symbol"]] = r
    except Exception as e:  # noqa: BLE001
        print(f"[preflight] could not read {TRADE_LOG}: {e}")
    return rows


def is_stale(row, today):
    """True if this position has outlived the hold its module allows.

    DAY_MOMENTUM: anything opened before today is stale, full stop.
    SWING_CATALYST: stale past SWING_MAX_HOLD_DAYS calendar days.
    Unknown provenance is treated as a day trade — the config has run in
    day-only mode since v2.1, so an untracked overnight position is an error
    by definition.
    """
    entry_date = (row or {}).get("date", "")
    if not entry_date or entry_date >= today:
        return False, ""
    module = (row or {}).get("module", "")
    if module == "SWING_CATALYST":
        try:
            d0 = datetime.fromisoformat(entry_date).date()
            d1 = datetime.fromisoformat(today).date()
            age = (d1 - d0).days
        except (ValueError, TypeError):
            return False, ""
        if age > getattr(config, "SWING_MAX_HOLD_DAYS", 5):
            return True, f"swing held {age}d > {config.SWING_MAX_HOLD_DAYS}d limit"
        return False, ""
    return True, f"day-module position opened {entry_date}, still open on {today}"


def close_stale(position, reason):
    """Cancel working sells, then close. Order matters — see protection_check."""
    sym = position["symbol"]
    try:
        protection_check.cancel_sell_orders(sym)
        executor.close_position(sym)
        print(f"[preflight] CLOSED {sym} — {reason}")
        return {"symbol": sym, "action": "closed", "reason": reason,
                "qty": position.get("qty"),
                "unrealized_pl": position.get("unrealized_pl")}
    except Exception as e:  # noqa: BLE001
        print(f"[preflight] CLOSE FAILED {sym}: {e}")
        return {"symbol": sym, "action": "close_failed", "reason": reason, "error": str(e)}


def run(close_stale_positions=True, dry_run=False):
    """Returns a result dict. Never raises — preflight must not be able to
    take down the morning run it is supposed to protect."""
    today = _today_et()
    result = {"timestamp": datetime.now(timezone.utc).isoformat(),
              "date_et": today, "protection": [], "stale": [], "errors": []}

    # 1. Restore protective stops first. If a position is about to be closed
    #    anyway this is harmless; if the close fails, it is essential.
    try:
        result["protection"] = protection_check.check(dry_run=dry_run)
    except Exception as e:  # noqa: BLE001
        result["errors"].append(f"protection_check: {e}")
        print(f"[preflight] protection check error (continuing): {e}")

    # 2. Close anything that has outlived its hold.
    try:
        positions = executor.get_open_positions()
    except Exception as e:  # noqa: BLE001
        result["errors"].append(f"get_open_positions: {e}")
        print(f"[preflight] could not read positions: {e}")
        positions = []

    rows = entry_rows_by_symbol()
    for p in positions:
        sym = p["symbol"]
        stale, reason = is_stale(rows.get(sym), today)
        if not stale:
            continue
        if dry_run or not close_stale_positions:
            result["stale"].append({"symbol": sym, "action": "would_close", "reason": reason})
            print(f"[preflight] {sym} would be closed — {reason}")
            continue
        result["stale"].append(close_stale(p, reason))
        time.sleep(0.5)

    if not dry_run:
        os.makedirs("logs", exist_ok=True)
        with open(PREFLIGHT_LOG, "a") as f:
            f.write(json.dumps(result) + "\n")

    closed = sum(1 for s in result["stale"] if s.get("action") == "closed")
    rearmed = sum(1 for a in result["protection"] if a.get("action") == "rearmed")
    failed = [s for s in result["stale"] if s.get("action") == "close_failed"]

    status = "OK"
    detail = f"{rearmed} stop(s) re-armed, {closed} stale position(s) closed"
    if failed:
        status = "ERROR"
        detail = f"{detail}; {len(failed)} close(s) FAILED"
    health.mark("preflight", status, detail)

    print(f"[preflight] {detail}")
    result["summary"] = detail
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Pre-guardrail remediation.")
    ap.add_argument("--dry-run", action="store_true", help="report only, place nothing")
    ap.add_argument("--sweep", action="store_true",
                    help="intraday sweep: re-arm stops AND close stale positions "
                         "(this is the default; the flag is explicit for the workflow)")
    ap.add_argument("--protect-only", action="store_true",
                    help="re-arm stops but do not close anything")
    args = ap.parse_args()
    if args.protect_only and args.sweep:
        ap.error("--protect-only and --sweep are contradictory")
    out = run(close_stale_positions=not args.protect_only, dry_run=args.dry_run)
    print()
    print(json.dumps(out, indent=2))
