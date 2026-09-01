"""
selftest.py — verify the fix logic offline, before it touches the account.

Runs with fake Alpaca responses and a temporary working directory. Places no
orders, reads no real account, needs no valid API keys. Run it once after
deploying and any time you change preflight/protection/health.

    python selftest.py

The one thing it cannot verify is Alpaca's actual behaviour on order
submission. Everything upstream of that — staleness rules, stop resolution,
heat arithmetic, health staleness, halt detection — is covered here.
"""

import os
import sys
import tempfile

os.environ.setdefault("ALPACA_API_KEY", "selftest")
os.environ.setdefault("ALPACA_SECRET_KEY", "selftest")

FAILURES = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        expected {want!r}, got {got!r}")
        FAILURES.append(label)


def main():
    repo = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, repo)
    workdir = tempfile.mkdtemp(prefix="clautrader-selftest-")
    os.chdir(workdir)
    os.makedirs("logs", exist_ok=True)

    import config
    import executor
    import health
    import preflight
    import protection_check

    print("\n[1] Stale-position rules")
    # A day-module position from a previous session must be flagged stale.
    stale, _ = preflight.is_stale(
        {"date": "2026-08-11", "module": "DAY_MOMENTUM"}, "2026-09-01")
    check("day position from a prior date is stale", stale, True)

    stale, _ = preflight.is_stale(
        {"date": "2026-09-01", "module": "DAY_MOMENTUM"}, "2026-09-01")
    check("day position opened today is not stale", stale, False)

    stale, _ = preflight.is_stale(
        {"date": "2026-08-28", "module": "SWING_CATALYST"}, "2026-09-01")
    check("swing inside its hold window is not stale", stale, False)

    stale, _ = preflight.is_stale(
        {"date": "2026-07-01", "module": "SWING_CATALYST"}, "2026-09-01")
    check("swing past SWING_MAX_HOLD_DAYS is stale", stale, True)

    stale, _ = preflight.is_stale(
        {"date": "2026-08-11", "module": ""}, "2026-09-01")
    check("unknown-module overnight position is stale", stale, True)

    check("missing row is not stale", preflight.is_stale(None, "2026-09-01")[0], False)

    print("\n[2] Stop resolution from the trade log")
    with open("logs/trade_log.csv", "w", newline="") as f:
        f.write("date,symbol,qty,ref_price,stop,order_id,skipped,module,time_in_force\n")
        f.write("2026-08-07,TWLO,61,238.68,225.92,abc,False,DAY_MOMENTUM,day\n")
        f.write("2026-08-11,FRMI,1775,6.945,6.39,def,False,DAY_MOMENTUM,day\n")
        f.write("2026-08-11,SKIPME,0,1.0,0.9,,True,DAY_MOMENTUM,day\n")
    check("recovers TWLO stop", protection_check.logged_stop_for("TWLO"), 225.92)
    check("recovers FRMI stop", protection_check.logged_stop_for("FRMI"), 6.39)
    check("ignores skipped rows", protection_check.logged_stop_for("SKIPME"), None)
    check("unknown symbol returns None", protection_check.logged_stop_for("NOPE"), None)

    print("\n[3] Heat arithmetic — the deadlock, reproduced and cleared")
    positions = [
        {"symbol": "TWLO", "qty": "61", "avg_entry_price": "238.68", "current_price": "230.00"},
        {"symbol": "NTRA", "qty": "47", "avg_entry_price": "310.905", "current_price": "300.00"},
        {"symbol": "FRMI", "qty": "1775", "avg_entry_price": "6.945", "current_price": "6.60"},
    ]
    executor.get_open_positions = lambda: positions

    # No stops present: every position is charged the 8% ceiling.
    executor.get_open_orders = lambda: []
    heat_unprotected = executor.current_portfolio_heat_pct(94971.0)
    over_cap = heat_unprotected >= config.MAX_PORTFOLIO_HEAT_PCT
    print(f"        unprotected heat = {heat_unprotected:.2f}% "
          f"(cap {config.MAX_PORTFOLIO_HEAT_PCT}%)")
    check("unprotected positions trip the heat cap (the bug)", over_cap, True)

    # Stops restored: heat reports the real entry->stop distance.
    executor.get_open_orders = lambda: [
        {"symbol": "TWLO", "side": "sell", "type": "stop", "stop_price": "225.92", "id": "1"},
        {"symbol": "NTRA", "side": "sell", "type": "stop", "stop_price": "295.47", "id": "2"},
        {"symbol": "FRMI", "side": "sell", "type": "stop", "stop_price": "6.39", "id": "3"},
    ]
    heat_protected = executor.current_portfolio_heat_pct(94971.0)
    print(f"        re-armed heat    = {heat_protected:.2f}%")
    check("re-arming stops brings heat back under the cap",
          heat_protected < config.MAX_PORTFOLIO_HEAT_PCT, True)
    check("re-armed heat is lower than the ceiling fallback",
          heat_protected < heat_unprotected, True)

    print("\n[4] Protection audit classifies correctly")
    executor.get_open_orders = lambda: [
        {"symbol": "TWLO", "side": "sell", "type": "stop", "stop_price": "225.92", "id": "1"},
    ]
    actions = {a["symbol"]: a["action"] for a in protection_check.check(dry_run=True)}
    check("already-stopped position reported protected", actions.get("TWLO"), "protected")
    check("unprotected position queued for re-arm", actions.get("FRMI"), "would_rearm")

    # A position already trading below its stop must be closed, not armed.
    positions.append({"symbol": "BUST", "qty": "10",
                      "avg_entry_price": "100.0", "current_price": "50.0"})
    with open("logs/trade_log.csv", "a", newline="") as f:
        f.write("2026-08-11,BUST,10,100.0,90.0,ghi,False,DAY_MOMENTUM,day\n")
    actions = {a["symbol"]: a["action"] for a in protection_check.check(dry_run=True)}
    check("position through its stop is flagged for closing",
          actions.get("BUST"), "beyond_stop")

    print("\n[5] Health ledger and staleness")
    check("trading_days_since counts weekdays only",
          health.trading_days_since("2026-08-28"), 2)  # Fri -> Tue
    health.mark("scan", "OK", "3 candidates")
    check("successful stage records last_ok",
          health.read()["stages"]["scan"]["last_ok"] is not None, True)

    health.mark("scan", "HALTED", "Portfolio heat cap reached")
    entry = health.read()["stages"]["scan"]
    check("halt does not advance last_ok", entry["last_ok"], health._today_et())
    check("halt status is recorded", entry["status"], "HALTED")

    # Simulate the real outage: last success three weeks ago, halting daily.
    data = health.read()
    data["stages"]["scan"]["last_ok"] = "2026-08-11"
    import json
    with open(health.HEALTH_JSON, "w") as f:
        json.dump(data, f)
    stale_stages = [s for s, *_ in health.stale(max_trading_days=2)]
    check("watchdog detects a stage that halts daily", "scan" in stale_stages, True)

    print("\n[6] Watchdog catches a workflow that never runs at all")
    import json as _json
    data = health.read()
    data["first_seen"] = "2026-08-01T00:00:00+00:00"
    data["stages"].pop("eod", None)
    with open(health.HEALTH_JSON, "w") as f:
        _json.dump(data, f)
    never = {stage: status for stage, _, status, _ in health.stale()}
    check("a stage with no health record is flagged NEVER_RAN",
          never.get("eod"), "NEVER_RAN")

    print("\n[7] Weekly review distinguishes halted from quiet")
    try:
        import self_review
    except ModuleNotFoundError as e:
        print(f"  SKIP  self_review import unavailable here ({e.name} not installed)")
    else:
        health.mark("scan", "HALTED", "Portfolio heat cap reached")
        check("halt reason surfaced to the reviewer",
              "heat" in self_review._halt_reason().lower(), True)
        health.mark("scan", "OK", "0 candidates")
        check("a genuinely quiet week reports no halt", self_review._halt_reason(), "")

    print()
    print("=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED: {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
