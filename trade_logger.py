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

import config


def _ensure_dirs():
    os.makedirs("logs", exist_ok=True)


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
    file_exists = os.path.exists(config.TRADE_LOG_CSV)
    with open(config.TRADE_LOG_CSV, "a", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "date", "symbol", "conviction", "catalyst_type",
                "reasoning", "key_risk", "qty", "ref_price",
                "stop", "target", "order_id", "skipped", "skip_reason",
            ],
        )
        if not file_exists:
            writer.writeheader()
        writer.writerow(
            {
                "date": datetime.now(timezone.utc).date().isoformat(),
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
            }
        )


def log_equity(account):
    """Append daily equity snapshot for the equity curve."""
    _ensure_dirs()
    file_exists = os.path.exists(config.EQUITY_LOG_CSV)
    with open(config.EQUITY_LOG_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "equity", "cash", "day_pnl_pct"])
        if not file_exists:
            writer.writeheader()
        equity = float(account["equity"])
        last = float(account["last_equity"]) or equity
        writer.writerow(
            {
                "date": datetime.now(timezone.utc).date().isoformat(),
                "equity": equity,
                "cash": account["cash"],
                "day_pnl_pct": round((equity - last) / last * 100, 3) if last else 0,
            }
        )
