"""
trade_logger.py — Logs every decision and every trade.

This dataset IS the experiment. Every Claude decision (including NO_TRADE
and every rejection reason) gets logged, so at the end you can analyze
what kinds of catalysts worked, not just the P&L.
"""

import csv
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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


def log_equity(account):
    """Record today's equity snapshot for the equity curve.

    One row per ET date: if today already has a row (morning run), it is
    REPLACED with this fresher reading (midday / evening), so the curve
    tracks the account instead of freezing at the morning print.
    """
    _ensure_dirs()
    fields = ["date", "equity", "cash", "day_pnl_pct"]
    today = _today_et()
    equity = float(account["equity"])
    last = float(account["last_equity"]) or equity
    new_row = {
        "date": today,
        "equity": equity,
        "cash": account["cash"],
        "day_pnl_pct": round((equity - last) / last * 100, 3) if last else 0,
    }
    rows = []
    if os.path.exists(config.EQUITY_LOG_CSV):
        with open(config.EQUITY_LOG_CSV, newline="") as f:
            rows = [r for r in csv.DictReader(f) if r.get("date") != today]
    rows.append(new_row)
    with open(config.EQUITY_LOG_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fields})
