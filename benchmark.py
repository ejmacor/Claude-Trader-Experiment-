"""
benchmark.py — Logs SPY's daily close so the dashboard can plot the market as
a benchmark line next to the account equity curve.

Uses the same free Alpaca IEX data feed as the scanner. One row per trading
day in logs/benchmark.csv. Firewalled by the caller — a failure here never
touches trading.

2026-09-02: the file froze at 2026-08-11 and stayed frozen for three weeks.
log_spy() only ever fetched /bars/latest — a single bar, today — so there was
no code path in the repo capable of filling a gap once one opened. The write
also sat behind an early exit in run_morning.py, so every skipped morning was
a permanently missing row. Both are fixed: fetch_range() can pull an arbitrary
window, backfill() merges it in without duplicating or disordering existing
rows, and the caller now writes the benchmark before any exit path.

    python benchmark.py                       # log today (same as before)
    python benchmark.py --backfill            # fill from last logged row to today
    python benchmark.py --backfill 2026-08-12 2026-09-01
    python benchmark.py --backfill ... --dry-run
"""

import argparse
import csv
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
ALPACA_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET = os.environ["ALPACA_SECRET_KEY"]
DATA_URL = "https://data.alpaca.markets"
BENCHMARK_CSV = "logs/benchmark.csv"
FIELDS = ["date", "spy_close"]

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}


def _today():
    return datetime.now(ET).date().isoformat()


def read_rows():
    """Existing rows as {date: spy_close}. A missing or corrupt file is empty —
    this module must never be the reason a run dies."""
    rows = {}
    if not os.path.exists(BENCHMARK_CSV):
        return rows
    try:
        with open(BENCHMARK_CSV, newline="") as f:
            for r in csv.DictReader(f):
                d, c = (r.get("date") or "").strip(), (r.get("spy_close") or "").strip()
                if d and c:
                    rows[d] = c
    except (OSError, csv.Error) as e:
        print(f"      benchmark: could not read {BENCHMARK_CSV} ({e})")
    return rows


def write_rows(rows):
    """Rewrite the whole file, sorted by date.

    A plain append cannot be used once backfilling exists: a row for 2026-08-12
    written today would land after 2026-08-11 in file order but the dashboard
    plots in file order, so the SPY line would zigzag backwards. Sorting on
    write makes insertion order irrelevant.
    """
    os.makedirs("logs", exist_ok=True)
    with open(BENCHMARK_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for d in sorted(rows):
            w.writerow({"date": d, "spy_close": rows[d]})


def fetch_latest():
    """SPY's most recent daily bar close."""
    resp = requests.get(
        f"{DATA_URL}/v2/stocks/SPY/bars/latest",
        headers=HEADERS, params={"feed": "iex"}, timeout=30,
    )
    resp.raise_for_status()
    return float(resp.json()["bar"]["c"])


def fetch_range(start, end):
    """{date: close} of SPY daily bars in [start, end], inclusive.

    Paginates — a multi-week window can exceed one response. Non-trading days
    simply do not come back, which is what we want: no synthetic rows.
    """
    out = {}
    page = None
    while True:
        params = {
            "timeframe": "1Day",
            "start": start,
            "end": end,
            "adjustment": "raw",
            "feed": "iex",
            "limit": 10000,
        }
        if page:
            params["page_token"] = page
        resp = requests.get(
            f"{DATA_URL}/v2/stocks/SPY/bars",
            headers=HEADERS, params=params, timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        for bar in body.get("bars") or []:
            ts = bar.get("t", "")
            day = ts[:10]
            if day and bar.get("c") is not None:
                out[day] = float(bar["c"])
        page = body.get("next_page_token")
        if not page:
            break
    return out


def log_spy():
    """Append today's SPY reference close. Idempotent for the day."""
    today = _today()
    rows = read_rows()
    if today in rows:
        return float(rows[today])
    close = fetch_latest()
    rows[today] = close
    write_rows(rows)
    return close


def backfill(start=None, end=None, dry_run=False):
    """Fill missing benchmark rows over a window.

    With no window, runs from the day after the last logged row through today,
    which is the normal way to close a gap after an outage. Existing rows are
    never overwritten — a backfill repairs history, it does not rewrite it.
    Returns the dates added.
    """
    rows = read_rows()
    if start is None:
        start = ((datetime.fromisoformat(max(rows)).date() + timedelta(days=1)).isoformat()
                 if rows else "2026-07-01")
    if end is None:
        end = _today()
    if start > end:
        print(f"      benchmark: nothing to backfill (start {start} > end {end})")
        return []

    print(f"      benchmark: fetching SPY daily bars {start} -> {end}")
    fetched = fetch_range(start, end)
    added = {d: c for d, c in fetched.items() if d not in rows}

    if not added:
        print(f"      benchmark: {len(fetched)} bar(s) returned, 0 new — already complete")
        return []

    print(f"      benchmark: {len(fetched)} bar(s) returned, {len(added)} new")
    for d in sorted(added):
        print(f"        + {d}  {added[d]}")
    if dry_run:
        print("      benchmark: --dry-run, nothing written")
        return sorted(added)

    rows.update(added)
    write_rows(rows)
    print(f"      benchmark: wrote {len(rows)} row(s) to {BENCHMARK_CSV}, sorted by date")
    return sorted(added)


def gaps(through=None):
    """Weekdays from the first logged row through TODAY that have no entry.

    Deliberately runs to today rather than to the last logged row. The gap this
    module exists to catch is a trailing one — the file stopped on 2026-08-11
    and the next three weeks are simply absent — and a first-to-last scan is
    blind to exactly that shape. Interior holes are caught too.

    Holidays show up here as false positives; this is a "look at this" signal,
    not an accounting record. Its job is to make a three-week hole impossible
    to miss.
    """
    rows = read_rows()
    if not rows:
        return []
    cur = datetime.fromisoformat(min(rows)).date()
    last = datetime.fromisoformat(through or _today()).date()
    out = []
    while cur < last:
        cur += timedelta(days=1)
        if cur.weekday() < 5 and cur.isoformat() not in rows:
            out.append(cur.isoformat())
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="SPY benchmark logger")
    p.add_argument("--backfill", nargs="*", metavar=("START", "END"),
                   help="fill missing rows; no args = from last logged row to today")
    p.add_argument("--dry-run", action="store_true", help="show what would be written")
    p.add_argument("--check", action="store_true", help="report weekday gaps and exit")
    a = p.parse_args(argv)

    if a.check:
        g = gaps()
        print(f"benchmark rows: {len(read_rows())}")
        print(f"weekday gaps  : {len(g)}" + (f"  ({g[0]} .. {g[-1]})" if g else ""))
        return 1 if g else 0

    if a.backfill is not None:
        start = a.backfill[0] if len(a.backfill) > 0 else None
        end = a.backfill[1] if len(a.backfill) > 1 else None
        backfill(start, end, dry_run=a.dry_run)
        return 0

    print(f"SPY {log_spy()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
