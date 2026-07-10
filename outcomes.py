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


def detect_swing_closes(still_open, seen):
    """Realized P&L for multi-day swings that closed, via their own bracket
    order legs. Same-day fill matching can never catch these: the buy fill
    is from a prior day, so today's orders only show the sell side.
    Returns {symbol: realized_pnl_pct}."""
    closes = {}
    if not os.path.exists("logs/trade_log.csv"):
        return closes
    with open("logs/trade_log.csv", newline="") as f:
        for r in csv.DictReader(f):
            sym = r.get("symbol", "")
            if r.get("skipped") in ("True", "true") or not r.get("order_id"):
                continue
            if r.get("time_in_force") != "gtc" and r.get("module") != "SWING_CATALYST":
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
        print("No decision logged for today; nothing to record.")
        return

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
        fills = {**detect_swing_closes(still_open, seen), **fills}

        # v2: swing positions closed today from PRIOR days' decisions — record
        # their realized round trips (today's decision won't contain them).
        for sym, pnl in fills.items():
            if sym not in taken and sym not in still_open \
               and (today_et(), sym, "SWING_CLOSED") not in seen:
                w.writerow({
                    "date": today_et(), "symbol": sym, "action": "SWING_CLOSED",
                    "conviction": "", "catalyst_type": "", "reject_reason": "",
                    "open_to_close_pct": "", "realized_pnl_pct": pnl,
                })
                print(f"SWING_CLOSED {sym:6s} realized {pnl}%")

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


if __name__ == "__main__":
    main()
