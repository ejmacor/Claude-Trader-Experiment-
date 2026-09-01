"""
manual_intervention.py — the operator's console.

When the automation is wedged, you need to see the account and act on it
without editing code or clicking through a broker UI at speed. Everything
here is explicit and requires you to type the command.

    python manual_intervention.py --status
    python manual_intervention.py --unstick --dry-run
    python manual_intervention.py --unstick
    python manual_intervention.py --flatten TWLO
    python manual_intervention.py --flatten-all
    python manual_intervention.py --rearm

--status is always safe and places no orders. Everything that transacts
prints what it is about to do and requires --yes or an interactive
confirmation, because a fat-fingered --flatten-all at 9:31am is its own
kind of incident.

Requires ALPACA_API_KEY and ALPACA_SECRET_KEY in the environment. Paper
account only — executor.BASE_URL is pinned to the paper endpoint.
"""

import argparse
import json
import sys

import config
import executor
import health
import preflight
import protection_check


def money(x):
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return str(x)


def cmd_status():
    acct = executor.get_account()
    equity = float(acct["equity"])
    positions = executor.get_open_positions()
    orders = executor.get_open_orders()
    stops = protection_check.live_sell_stops()

    print("=" * 68)
    print("ACCOUNT")
    print("=" * 68)
    print(f"  equity        {money(equity)}")
    print(f"  cash          {money(acct['cash'])}")
    print(f"  last_equity   {money(acct['last_equity'])}")

    print()
    print("=" * 68)
    print(f"POSITIONS ({len(positions)})")
    print("=" * 68)
    if not positions:
        print("  (flat)")
    for p in positions:
        sym = p["symbol"]
        stop = stops.get(sym)
        flag = "" if stop else "   <-- NO STOP"
        print(f"  {sym:6s} qty {p['qty']:>7} @ {money(p['avg_entry_price'])} "
              f"last {money(p.get('current_price'))} "
              f"unrealized {money(p.get('unrealized_pl'))}{flag}")
        if stop:
            print(f"         stop {money(stop)}")

    print()
    print("=" * 68)
    print(f"WORKING ORDERS ({len(orders)})")
    print("=" * 68)
    if not orders:
        print("  (none)")
    for o in orders:
        print(f"  {o['symbol']:6s} {o.get('side'):4s} {o.get('type'):10s} "
              f"qty {o.get('qty')} stop {o.get('stop_price')} "
              f"limit {o.get('limit_price')} tif {o.get('time_in_force')}")

    # The number that actually decides whether the bot may trade tomorrow.
    print()
    print("=" * 68)
    print("GUARDRAIL VIEW")
    print("=" * 68)
    try:
        heat = executor.current_portfolio_heat_pct(equity)
        cap = config.MAX_PORTFOLIO_HEAT_PCT
        verdict = "AT/OVER CAP — new trades BLOCKED" if heat >= cap else "under cap"
        print(f"  portfolio heat   {heat:.2f}%  (cap {cap:.2f}%)  -> {verdict}")
        if heat >= cap:
            unprotected = [p["symbol"] for p in positions if p["symbol"] not in stops]
            if unprotected:
                print(f"  positions with no live stop: {unprotected}")
                print(f"  each is charged the {config.STOP_PCT_CEIL}% ceiling instead of its")
                print("  real entry->stop distance. Run --rearm to report the truth.")
    except Exception as e:  # noqa: BLE001
        print(f"  heat calculation failed: {e}")

    print(f"  open positions   {len(positions)} (max {config.MAX_OPEN_POSITIONS})")

    print()
    print("=" * 68)
    print("PIPELINE HEALTH")
    print("=" * 68)
    data = health.read()
    if not data["stages"]:
        print("  (no health data — deploy the fix and let one morning run write it)")
    for stage, entry in data["stages"].items():
        days = health.trading_days_since(entry.get("last_ok"))
        age = "never" if days > 900 else f"{days}d ago"
        print(f"  {stage:15s} {entry.get('status', '?'):8s} last OK {age:12s} "
              f"{entry.get('detail', '')[:60]}")


def confirm(prompt, assume_yes):
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [type YES to proceed]: ").strip() == "YES"
    except (EOFError, KeyboardInterrupt):
        return False


def cmd_flatten(symbols, assume_yes):
    positions = {p["symbol"]: p for p in executor.get_open_positions()}
    targets = [s for s in symbols if s in positions]
    missing = [s for s in symbols if s not in positions]
    for s in missing:
        print(f"  {s}: no open position, skipping")
    if not targets:
        print("Nothing to close.")
        return
    print("About to CLOSE at market:")
    for s in targets:
        p = positions[s]
        print(f"  {s} qty {p['qty']} unrealized {money(p.get('unrealized_pl'))}")
    if not confirm("Proceed?", assume_yes):
        print("Aborted.")
        return
    for s in targets:
        protection_check.cancel_sell_orders(s)
        try:
            executor.close_position(s)
            print(f"  CLOSED {s}")
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED {s}: {e}")


def main():
    ap = argparse.ArgumentParser(description="Manual operator controls (paper account).")
    ap.add_argument("--status", action="store_true", help="print account, positions, heat, health")
    ap.add_argument("--rearm", action="store_true", help="restore protective stops on unprotected positions")
    ap.add_argument("--unstick", action="store_true",
                    help="full preflight: re-arm stops AND close positions past their hold")
    ap.add_argument("--flatten", metavar="SYM", nargs="+", help="close specific positions")
    ap.add_argument("--flatten-all", action="store_true", help="close every open position")
    ap.add_argument("--dry-run", action="store_true", help="report only, place nothing")
    ap.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    args = ap.parse_args()

    if not any([args.status, args.rearm, args.unstick, args.flatten, args.flatten_all]):
        ap.print_help()
        return 1

    if args.status:
        cmd_status()

    if args.rearm:
        print("\nRe-arming protective stops...")
        print(json.dumps(protection_check.check(dry_run=args.dry_run), indent=2))

    if args.unstick:
        print("\nRunning full preflight remediation...")
        print(json.dumps(preflight.run(dry_run=args.dry_run), indent=2))

    if args.flatten:
        cmd_flatten([s.upper() for s in args.flatten], args.yes)

    if args.flatten_all:
        positions = executor.get_open_positions()
        if not positions:
            print("Already flat.")
        else:
            cmd_flatten([p["symbol"] for p in positions], args.yes)

    return 0


if __name__ == "__main__":
    sys.exit(main())
