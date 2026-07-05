"""
benchmark.py — Logs SPY's latest daily close so the dashboard can plot
the market as a benchmark line next to the account equity curve.

Uses the same free Alpaca IEX data feed as the scanner. One row per run
day in logs/benchmark.csv. Firewalled by the caller — a failure here
never touches trading.
"""

import csv
import os
from datetime import datetime, timezone

import requests

ALPACA_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET = os.environ["ALPACA_SECRET_KEY"]
DATA_URL = "https://data.alpaca.markets"
BENCHMARK_CSV = "logs/benchmark.csv"

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}


def log_spy():
    """Append today's SPY reference price (latest daily bar close)."""
    resp = requests.get(
        f"{DATA_URL}/v2/stocks/SPY/bars/latest",
        headers=HEADERS, params={"feed": "iex"}, timeout=30,
    )
    resp.raise_for_status()
    close = float(resp.json()["bar"]["c"])

    today = datetime.now(timezone.utc).date().isoformat()
    os.makedirs("logs", exist_ok=True)

    # one row per date — skip if today already logged
    if os.path.exists(BENCHMARK_CSV):
        with open(BENCHMARK_CSV, newline="") as f:
            if any(r.get("date") == today for r in csv.DictReader(f)):
                return close

    file_exists = os.path.exists(BENCHMARK_CSV)
    with open(BENCHMARK_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "spy_close"])
        if not file_exists:
            w.writeheader()
        w.writerow({"date": today, "spy_close": close})
    return close
