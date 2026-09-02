"""
trade_logger.py — Logs every decision and every trade.

This dataset IS the experiment. Every Claude decision (including NO_TRADE
and every rejection reason) gets logged, so at the end you can analyze
what kinds of catalysts worked, not just the P&L.
"""

import csv
import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import config

ET = ZoneInfo("America/New_York")

TRADE_FIELDS = [
    "date", "symbol", "conviction", "catalyst_type",
    "reasoning", "key_risk", "qty", "ref_price",
    "stop", "target", "order_id", "skipped", "skip_reason",
    "module", "time_in_force",
]


def _ensure_dirs():
    os.makedirs("logs", exist_ok=True)


def _today_et():
    """All log dates are ET. A cron delayed past 8pm ET would otherwise
    roll the UTC date forward and break every date|symbol join downstream."""
    return datetime.now(ET).date().isoformat()


def _migrate_header(path, fieldnames):
    """If the CSV on disk has an older, shorter header, rewrite it with the
    new columns appended (blank for old rows) so DictWriter stays aligned."""
    if not os.path.exists(path):
        return
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        existing = reader.fieldnames
        rows = list(reader)
    if existing is None or list(existing) == list(fieldnames):
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def log_decision(candidates, decision):
    """Append the full morning decision (inputs + outputs) as one JSONL row."""
    _ensure_dirs()
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "candidates_sent": [
            {"symbol": c["symbol"], "gap_pct": c["gap_pct"], "headlines": [n["headline"] for n in c["news"]]}
            for c in candidates
        ],
        "decision": decision,
    }
    with open(config.DECISION_LOG_JSONL, "a") as f:
        f.write(json.dumps(row) + "\n")


def log_trade(trade_plan, execution):
    """Append an executed (or skipped) trade to the CSV log."""
    _ensure_dirs()
    _migrate_header(config.TRADE_LOG_CSV, TRADE_FIELDS)
    file_exists = os.path.exists(config.TRADE_LOG_CSV)
    with open(config.TRADE_LOG_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(
            {
                "date": _today_et(),
                "symbol": trade_plan.get("symbol", ""),
                "conviction": trade_plan.get("conviction", ""),
                "catalyst_type": trade_plan.get("catalyst_type", ""),
                "reasoning": trade_plan.get("reasoning", ""),
                "key_risk": trade_plan.get("key_risk", ""),
                "qty": execution.get("qty", ""),
                "ref_price": execution.get("ref_price", ""),
                "stop": execution.get("stop", ""),
                "target": execution.get("target", ""),
                "order_id": execution.get("order_id", ""),
                "skipped": execution.get("skipped", False),
                "skip_reason": execution.get("reason", ""),
                "module": execution.get("module", trade_plan.get("module", "")),
                "time_in_force": execution.get("time_in_force", ""),
            }
        )


EQUITY_FIELDS = ["date", "equity", "cash", "day_pnl_pct"]
ALPACA_BASE = "https://paper-api.alpaca.markets"

# Gap between a stored intraday snapshot and the settled close big enough to
# be worth printing. NOT a correction threshold — the close is authoritative
# and always wins. This only controls how loud we are about it.
EQUITY_DRIFT_NOTABLE_PCT = 2.0


def _alpaca_headers():
    return {
        "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
    }


def _prev_weekday(d):
    """The weekday before d. Holidays are not excluded — this only decides
    whether a correction is SAFE to apply, and a holiday makes it skip, which
    is the fail-safe direction."""
    p = d - timedelta(days=1)
    while p.weekday() >= 5:
        p -= timedelta(days=1)
    return p


def read_equity_rows():
    """Stored equity rows, oldest first. A missing/corrupt file is empty."""
    if not os.path.exists(config.EQUITY_LOG_CSV):
        return []
    try:
        with open(config.EQUITY_LOG_CSV, newline="") as f:
            return [r for r in csv.DictReader(f) if r.get("date")]
    except (OSError, csv.Error) as e:
        print(f"      equity: could not read log ({e})")
        return []


def write_equity_rows(rows):
    _ensure_dirs()
    with open(config.EQUITY_LOG_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EQUITY_FIELDS)
        w.writeheader()
        for r in sorted(rows, key=lambda x: x.get("date", "")):
            w.writerow({k: r.get(k, "") for k in EQUITY_FIELDS})


def settle_previous_close(rows, today, last_equity):
    """Replace yesterday's stored snapshot with its settled close.

    THE PROBLEM THIS FIXES (found 2026-09-02): every row in the curve is an
    INTRADAY reading — log_equity writes account["equity"] at whatever moment
    the run fires, and the last run of the day wins. Usually harmless, landing
    within ~0.5% of the close. Once it was not: the 2026-07-28 row stored
    84,615.95 while Alpaca's last_equity the next morning said 97,056.69. Cash
    was unchanged all week, so nothing was sold and the account never closed
    anywhere near 84,616.

    That one row set MAX DRAWDOWN to -16.26%. Without it the figure is -6.94%.
    A bad intraday mark was driving the headline risk number on the dashboard.

    The fix is not a threshold — thresholds on this data are guesswork, and a
    0.25% one flagged eleven perfectly normal rows. `last_equity` IS the
    settled prior close, so it simply wins, unconditionally. Today's row stays
    a live snapshot (the dashboard needs that); every prior row becomes a true
    close the next morning.

    Only fires when the newest stored row is the immediately preceding
    weekday, so a gap in the log can never overwrite an old row with a much
    later close.
    """
    if not rows or not last_equity:
        return rows, None
    prev = rows[-1]
    try:
        want = _prev_weekday(datetime.fromisoformat(today).date()).isoformat()
    except (TypeError, ValueError):
        return rows, None
    if prev.get("date") != want:
        return rows, None
    try:
        stored = float(prev.get("equity"))
    except (TypeError, ValueError):
        return rows, None
    if not stored:
        return rows, None

    drift = abs(stored - last_equity) / last_equity * 100
    prev["equity"] = last_equity
    prev["day_pnl_pct"] = ""   # derived from the snapshot; no longer true
    if drift > EQUITY_DRIFT_NOTABLE_PCT:
        print(f"      equity: {prev['date']} snapshot {stored:,.2f} was "
              f"{drift:.2f}% off the settled close {last_equity:,.2f} — corrected")
    return rows, {"date": prev["date"], "was": stored,
                  "now": last_equity, "drift_pct": round(drift, 3)}


def log_equity(account):
    """Record today's equity snapshot, and settle yesterday's close.

    One row per ET date: if today already has a row (morning run), it is
    REPLACED with this fresher reading (midday / evening), so the curve
    tracks the account instead of freezing at the morning print.
    """
    _ensure_dirs()
    today = _today_et()
    equity = float(account["equity"])
    last = float(account["last_equity"]) or equity
    new_row = {
        "date": today,
        "equity": equity,
        "cash": account["cash"],
        "day_pnl_pct": round((equity - last) / last * 100, 3) if last else 0,
    }
    rows = [r for r in read_equity_rows() if r.get("date") != today]
    rows, _ = settle_previous_close(rows, today, float(account["last_equity"] or 0))
    rows.append(new_row)
    write_equity_rows(rows)


def fetch_portfolio_history(period="3M"):
    """{date: equity} from Alpaca's own daily portfolio history.

    The account's real closing series — not our sampled snapshots — and the
    only way to repair history that was already written badly.
    """
    resp = requests.get(
        f"{ALPACA_BASE}/v2/account/portfolio/history",
        headers=_alpaca_headers(),
        params={"period": period, "timeframe": "1D", "extended_hours": "false"},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    out = {}
    for ts, eq in zip(body.get("timestamp") or [], body.get("equity") or []):
        if eq is None:
            continue
        day = datetime.fromtimestamp(ts, ZoneInfo("America/New_York")).date().isoformat()
        out[day] = round(float(eq), 2)
    return out


def repair_equity(period="3M", dry_run=False):
    """Rewrite stored closes from Alpaca's portfolio history.

    Only touches dates BEFORE today — today's row is a live snapshot by design
    and must keep tracking the account. Cash is left alone; the history
    endpoint does not report it and our stored value is correct.
    """
    truth = fetch_portfolio_history(period)
    if not truth:
        print("equity: portfolio history returned nothing")
        return []
    rows = read_equity_rows()
    today = _today_et()
    changed = []
    for r in rows:
        d = r["date"]
        if d >= today or d not in truth:
            continue
        try:
            stored = float(r["equity"])
        except (TypeError, ValueError):
            continue
        real = truth[d]
        if not real or abs(stored - real) / real * 100 <= 0.01:
            continue
        changed.append((d, stored, real, round(abs(stored - real) / real * 100, 2)))
        if not dry_run:
            r["equity"] = real
            r["day_pnl_pct"] = ""
    if not changed:
        print("equity: already matches Alpaca's portfolio history")
        return []
    changed.sort(key=lambda x: -x[3])
    print(f"equity: {len(changed)} row(s) differ from Alpaca's record (largest first)")
    for d, s, real, drift in changed[:15]:
        print(f"  {d}  stored {s:>12,.2f}  actual {real:>12,.2f}  off {drift:>6.2f}%")
    if dry_run:
        print("equity: --dry-run, nothing written")
        return changed
    write_equity_rows(rows)
    print(f"equity: rewrote {len(changed)} row(s)")
    return changed


def max_drawdown(rows=None):
    """(max_dd_pct, trough_date) over the stored curve. Reported by the CLI so
    the effect of a repair on the headline risk number is visible."""
    rows = rows if rows is not None else read_equity_rows()
    peak, dd, at = None, 0.0, None
    for r in rows:
        try:
            e = float(r["equity"])
        except (TypeError, ValueError):
            continue
        peak = e if peak is None else max(peak, e)
        if peak and (e - peak) / peak < dd:
            dd, at = (e - peak) / peak, r["date"]
    return round(dd * 100, 2), at


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="equity curve integrity")
    p.add_argument("--repair", action="store_true",
                   help="rewrite stored closes from Alpaca portfolio history")
    p.add_argument("--period", default="3M", help="history window (default 3M)")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    dd, at = max_drawdown()
    print(f"rows: {len(read_equity_rows())}   max drawdown {dd}%  (trough {at})")
    if a.repair or a.dry_run:
        print()
        repair_equity(period=a.period, dry_run=a.dry_run)
        if not a.dry_run:
            dd2, at2 = max_drawdown()
            print(f"\nmax drawdown now {dd2}%  (trough {at2})")
