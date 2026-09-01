"""
outcomes.py — Run after the close. Records how today's decisions played out:
- Trades TAKEN: entry -> exit result from Alpaca order history
- Trades REJECTED: what the stock did open-to-close anyway (the counterfactual)

This builds the learning dataset. It never influences decisions during the
frozen 90-day run — it only measures them. Output: logs/outcomes.csv
"""

import csv
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

import executor

DATA_URL = "https://data.alpaca.markets"
OUTCOMES_CSV = "logs/outcomes.csv"
DECISIONS_JSONL = "logs/decisions.jsonl"


def today_et():
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def load_todays_decision():
    """Last decision entry from today (ET), or None."""
    if not os.path.exists(DECISIONS_JSONL):
        return None
    todays = None
    with open(DECISIONS_JSONL) as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = row.get("timestamp", "")
            try:
                d = datetime.fromisoformat(ts).astimezone(ZoneInfo("America/New_York")).date().isoformat()
            except ValueError:
                continue
            if d == today_et():
                todays = row
    return todays


def get_open_close(symbol):
    """Today's official open and latest close price for a symbol (IEX feed)."""
    resp = requests.get(
        f"{DATA_URL}/v2/stocks/{symbol}/bars",
        headers=executor.HEADERS,
        params={"timeframe": "1Day", "start": today_et(), "feed": "iex", "limit": 1},
        timeout=30,
    )
    resp.raise_for_status()
    bars = resp.json().get("bars") or []
    if not bars:
        return None, None
    return bars[0].get("o"), bars[0].get("c")


def get_todays_fills():
    """Map symbol -> realized round-trip P&L pct from today's filled orders."""
    et_start = datetime.combine(
        datetime.now(ZoneInfo("America/New_York")).date(),
        datetime.min.time(),
        tzinfo=ZoneInfo("America/New_York"),
    ).isoformat()  # correct offset year-round; was hardcoded -04:00 (breaks under EST)
    resp = requests.get(
        f"{executor.BASE_URL}/v2/orders",
        headers=executor.HEADERS,
        params={"status": "closed", "after": et_start, "limit": 200, "direction": "asc"},
        timeout=30,
    )
    resp.raise_for_status()
    orders = [o for o in resp.json() if o.get("filled_at")]

    by_symbol = {}
    for o in orders:
        s = o["symbol"]
        by_symbol.setdefault(s, {"buy_cost": 0.0, "sell_proceeds": 0.0, "bought": 0.0, "sold": 0.0})
        px = float(o["filled_avg_price"])
        q = float(o["filled_qty"])
        if o["side"] == "buy":
            by_symbol[s]["buy_cost"] += px * q
            by_symbol[s]["bought"] += q
        else:
            by_symbol[s]["sell_proceeds"] += px * q
            by_symbol[s]["sold"] += q

    result = {}
    for s, v in by_symbol.items():
        if v["bought"] > 0 and abs(v["bought"] - v["sold"]) < 1e-6:
            result[s] = round((v["sell_proceeds"] - v["buy_cost"]) / v["buy_cost"] * 100, 2)
    return result


def open_position_symbols():
    try:
        return {p["symbol"] for p in executor.get_open_positions()}
    except Exception:  # noqa: BLE001
        return set()


def _et_day(ts):
    """ET calendar date of an Alpaca UTC timestamp. The exit is dated when it
    actually happened, not when the recorder noticed it — a backfilled close
    stamped with today's date would silently corrupt every date|symbol join."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(
            ZoneInfo("America/New_York")).date().isoformat()
    except (ValueError, TypeError, AttributeError):
        return today_et()


def detect_swing_closes(still_open, seen, out_dates=None):
    """Realized P&L for any position opened on a PRIOR day that is now closed.

    Same-day fill matching can never catch these: the buy fill is from an
    earlier session, so today's order list only shows the sell side. Applies
    to day-module positions that overstayed as well as genuine swings.
    Returns {symbol: realized_pnl_pct}."""
    closes = {}
    if out_dates is None:
        out_dates = {}
    if not os.path.exists("logs/trade_log.csv"):
        return closes
    with open("logs/trade_log.csv", newline="") as f:
        for r in csv.DictReader(f):
            sym = r.get("symbol", "")
            if r.get("skipped") in ("True", "true") or not r.get("order_id"):
                continue
            # v2.1 FIX: this used to require gtc / SWING_CATALYST, which meant
            # a DAY_MOMENTUM position that survived past its entry date could
            # NEVER have its exit recorded. Those are exactly the positions
            # that go wrong (see the 2026-08 stranded inventory), so they are
            # the ones whose realized P&L matters most. Any position whose
            # entry date is in the past and which is no longer open is now a
            # candidate for exit reconstruction.
            entry_date = r.get("date", "")
            if not entry_date or entry_date >= today_et():
                continue
            if sym in still_open:
                continue  # still holding — nothing realized yet
            if any(s == sym and a == "SWING_CLOSED" for (_, s, a) in seen):
                continue  # already recorded
            try:
                parent = requests.get(
                    f"{executor.BASE_URL}/v2/orders/{r['order_id']}",
                    headers=executor.HEADERS, params={"nested": "true"}, timeout=30,
                ).json()
                buy_px = float(parent.get("filled_avg_price") or 0)
                sell_px = 0.0
                for leg in parent.get("legs") or []:
                    if leg.get("side") == "sell" and leg.get("filled_avg_price"):
                        sell_px = float(leg["filled_avg_price"])
                        if leg.get("filled_at"):
                            out_dates[sym] = _et_day(leg["filled_at"])
                # Fallback: EOD flatten / midday close CANCELS the bracket legs
                # and exits via a separate market sell — find that fill instead.
                if buy_px and not sell_px:
                    closed_orders = requests.get(
                        f"{executor.BASE_URL}/v2/orders",
                        headers=executor.HEADERS,
                        params={"status": "closed", "symbols": sym,
                                "after": f"{r.get('date','')}T00:00:00-05:00",
                                "limit": 100, "direction": "desc"},
                        timeout=30,
                    ).json()
                    qty_needed = float(r.get("qty") or 0)
                    sold_val = sold_qty = 0.0
                    for o in closed_orders:
                        if o.get("side") == "sell" and o.get("filled_avg_price"):
                            q = float(o.get("filled_qty") or 0)
                            sold_val += float(o["filled_avg_price"]) * q
                            sold_qty += q
                            if o.get("filled_at"):
                                out_dates[sym] = _et_day(o["filled_at"])
                    if sold_qty and (not qty_needed or sold_qty >= qty_needed):
                        sell_px = sold_val / sold_qty
                if buy_px and sell_px:
                    closes[sym] = round((sell_px - buy_px) / buy_px * 100, 2)
            except Exception as e:  # noqa: BLE001
                print(f"swing close lookup failed for {sym}: {e}")
    return closes


def already_recorded():
    """(date, symbol, action) triples already in outcomes.csv — makes reruns
    (backup crons, manual dispatches) append-safe instead of duplicating."""
    seen = set()
    if os.path.exists(OUTCOMES_CSV):
        with open(OUTCOMES_CSV, newline="") as f:
            for r in csv.DictReader(f):
                seen.add((r.get("date"), r.get("symbol"), r.get("action")))
    return seen


def safe_open_close(symbol):
    """One bad ticker's data request must never kill the whole recorder."""
    try:
        return get_open_close(symbol)
    except Exception as e:  # noqa: BLE001
        print(f"open/close fetch failed for {symbol} (skipping pct): {e}")
        return None, None


def main():
    # Evening equity snapshot — replaces today's morning row with the
    # post-close reading so the dashboard matches the Alpaca account.
    try:
        import trade_logger
        trade_logger.log_equity(executor.get_account())
    except Exception as e:  # noqa: BLE001
        print(f"Equity snapshot failed (ignored): {e}")

    decision_row = load_todays_decision()
    if decision_row is None:
        # v2.1 FIX: this used to `return`. That is why six closed trades have
        # no realized P&L in the ledger — they exited on days when no new
        # decision was logged (2026-08-06 liquidated four positions and
        # recorded nothing). Exits happen independently of new decisions, so
        # the recorder now continues with an empty decision set and still
        # reconstructs closes.
        print("No decision logged for today — continuing to record exits only.")
        decision_row = {"decision": {}}

    decision = decision_row.get("decision", {})
    taken = {t["symbol"]: t for t in decision.get("trades", [])}
    rejected = {r["symbol"]: r for r in decision.get("rejected", [])}
    fills = get_todays_fills()
    seen = already_recorded()

    file_exists = os.path.exists(OUTCOMES_CSV)
    os.makedirs("logs", exist_ok=True)
    with open(OUTCOMES_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "date", "symbol", "action", "conviction", "catalyst_type",
            "reject_reason", "open_to_close_pct", "realized_pnl_pct",
        ])
        if not file_exists:
            w.writeheader()

        still_open = open_position_symbols()
        close_dates = {}
        fills = {**detect_swing_closes(still_open, seen, close_dates), **fills}

        # v2: swing positions closed today from PRIOR days' decisions — record
        # their realized round trips (today's decision won't contain them).
        for sym, pnl in fills.items():
            # De-dupe on symbol across ALL dates: a close backfilled with its
            # true exit date would otherwise be re-recorded every evening,
            # because the old guard only checked today's date.
            already = any(s == sym and a == "SWING_CLOSED" for (_, s, a) in seen)
            if sym not in taken and sym not in still_open and not already:
                w.writerow({
                    "date": close_dates.get(sym, today_et()),
                    "symbol": sym, "action": "SWING_CLOSED",
                    "conviction": "", "catalyst_type": "", "reject_reason": "",
                    "open_to_close_pct": "", "realized_pnl_pct": pnl,
                })
                print(f"SWING_CLOSED {sym:6s} realized {pnl}% "
                      f"(exit {close_dates.get(sym, today_et())})")

        for sym, t in taken.items():
            action = "OPEN_SWING" if sym in still_open else "TAKEN"
            if (today_et(), sym, action) in seen:
                continue
            o, c = safe_open_close(sym)
            oc = round((c - o) / o * 100, 2) if o and c else ""
            w.writerow({
                "date": today_et(), "symbol": sym, "action": action,
                "conviction": t.get("conviction", ""),
                "catalyst_type": t.get("catalyst_type", ""),
                "reject_reason": "",
                "open_to_close_pct": oc,
                "realized_pnl_pct": fills.get(sym, ""),
            })
            print(f"{action:9s}{sym:6s} open->close {oc}%  realized {fills.get(sym, chr(39)+chr(110)+chr(47)+chr(97)+chr(39))}%")

        for sym, r in rejected.items():
            if (today_et(), sym, "REJECTED") in seen:
                continue
            o, c = safe_open_close(sym)
            oc = round((c - o) / o * 100, 2) if o and c else ""
            w.writerow({
                "date": today_et(), "symbol": sym, "action": "REJECTED",
                "conviction": "", "catalyst_type": "",
                "reject_reason": r.get("reason", ""),
                "open_to_close_pct": oc,
                "realized_pnl_pct": "",
            })
            print(f"REJECTED {sym:6s} open->close {oc}%  ({r.get('reason', '')[:60]})")

    print(f"\nOutcomes appended to {OUTCOMES_CSV}")
    try:
        import health
        health.mark("evening", "OK",
                    f"{len(taken)} taken / {len(rejected)} rejected / {len(fills)} exit(s) reconstructed")
    except Exception as e:  # noqa: BLE001
        print(f"health mark failed (ignored): {e}")


if __name__ == "__main__":
    main()
