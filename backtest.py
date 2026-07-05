"""
backtest.py — Historical validation layer for the v2 engine.

Simulates the mechanical core of the strategy (gap entry + ATR bracket)
over historical daily bars so filter/parameter changes are tested against
data instead of vibes. Claude's catalyst judgment cannot be backtested,
so this measures the FLOOR: the strategy with zero news selectivity.
If the floor is breakeven-ish and live results beat it, the judgment
layer is adding value — that's the experiment's core question, isolated.

Method (daily bars only — honest about its limits):
- Universe: liquid names list below (edit freely).
- Signal day: open gaps >= MIN_GAP_PCT vs prior close.
- Entry at open. Bracket = ATR(14)-scaled stop/target (same clamps as live).
- Intraday path is unknown from daily bars, so resolution is PESSIMISTIC:
  if the day's range touches both stop and target, the stop is assumed
  to fill first. Day variant exits at close if neither hit; swing variant
  carries the bracket up to SWING_MAX_HOLD_DAYS.
- Costs: SLIPPAGE_BPS charged per side.
- Regime tag per trade from SPY 200-SMA (bull/bear) for grouped stats.

Run:  python backtest.py            (last ~2 years, both variants)
Output: printed report + logs/backtest_results.csv
"""

import csv
import os
import statistics
from datetime import datetime, timedelta, timezone

import requests

import config

DATA_URL = "https://data.alpaca.markets"
SLIPPAGE_BPS = 10          # 0.10% per side — conservative for liquid names
LOOKBACK_DAYS = 730
RESULTS_CSV = "logs/backtest_results.csv"

UNIVERSE = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AMD","AVGO","QCOM",
    "SMCI","PLTR","COIN","MARA","RIOT","SOFI","HOOD","DKNG","RBLX","U",
    "CRWD","NET","DDOG","SNOW","MDB","ZS","PANW","SHOP","SQ","PYPL",
    "UBER","LYFT","ABNB","DASH","CVNA","AFRM","UPST","AI","IONQ","RGTI",
    "MRNA","NVAX","SAVA","IOVA","VKTX","CELH","ELF","ANF","GPS","M",
    "F","GM","RIVN","LCID","NIO","XPEV","BABA","JD","PDD","BIDU",
]


def _headers():
    return {
        "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
    }


def get_bars(symbol, days=LOOKBACK_DAYS):
    start = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    out, page = [], None
    while True:
        params = {"timeframe": "1Day", "start": start, "feed": "iex", "limit": 1000}
        if page:
            params["page_token"] = page
        r = requests.get(f"{DATA_URL}/v2/stocks/{symbol}/bars", headers=_headers(),
                         params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        out += j.get("bars") or []
        page = j.get("next_page_token")
        if not page:
            return out


def atr(bars, i, n=14):
    if i < n:
        return None
    trs = []
    for k in range(i - n, i):
        h, l, pc = bars[k]["h"], bars[k]["l"], bars[k - 1]["c"] if k else bars[k]["o"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / n


def spy_bull_map():
    """date -> True if SPY closed above its 200-SMA (bull regime)."""
    bars = get_bars("SPY", LOOKBACK_DAYS + 320)
    closes = [b["c"] for b in bars]
    out = {}
    for i, b in enumerate(bars):
        if i >= 200:
            out[b["t"][:10]] = closes[i] > sum(closes[i - 200:i]) / 200
    return out


def simulate(symbol, bars, bull, variant):
    """Yield trade dicts for one symbol. variant: 'day' or 'swing'."""
    for i in range(15, len(bars) - config.SWING_MAX_HOLD_DAYS - 1):
        prev_c, o = bars[i - 1]["c"], bars[i]["o"]
        gap = (o - prev_c) / prev_c * 100
        if gap < config.MIN_GAP_PCT or not (config.MIN_PRICE <= o <= config.MAX_PRICE):
            continue
        a = atr(bars, i)
        if not a:
            continue
        stop_dist = max(o * config.STOP_PCT_FLOOR / 100,
                        min(a * config.STOP_ATR_MULT, o * config.STOP_PCT_CEIL / 100))
        stop = o - stop_dist
        target = o + stop_dist * (config.TARGET_ATR_MULT / config.STOP_ATR_MULT)

        exit_px, exit_kind, hold = None, None, 0
        horizon = 1 if variant == "day" else config.SWING_MAX_HOLD_DAYS
        for d in range(horizon):
            bar = bars[i + d]
            hold = d + 1
            hit_stop, hit_target = bar["l"] <= stop, bar["h"] >= target
            if hit_stop:                # pessimistic: stop first when both touch
                exit_px, exit_kind = stop, "stop"
                break
            if hit_target:
                exit_px, exit_kind = target, "target"
                break
        if exit_px is None:
            exit_px, exit_kind = bars[i + horizon - 1]["c"], "time"

        cost = (SLIPPAGE_BPS / 10_000) * 2
        pnl_pct = (exit_px - o) / o * 100 - cost * 100
        yield {
            "symbol": symbol, "date": bars[i]["t"][:10], "variant": variant,
            "gap_pct": round(gap, 1), "hold_days": hold, "exit": exit_kind,
            "pnl_pct": round(pnl_pct, 2), "r_multiple": round((exit_px - o) / stop_dist, 2),
            "regime": "bull" if bull.get(bars[i]["t"][:10]) else "bear",
        }


def report(trades, label):
    if not trades:
        print(f"\n{label}: no trades")
        return
    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) else float("inf")
    expectancy = statistics.mean(pnls)
    # max drawdown of the sequential per-trade equity curve
    eq, peak, mdd = 100.0, 100.0, 0.0
    for p in sorted(trades, key=lambda t: t["date"]):
        eq *= 1 + p["pnl_pct"] / 100
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100)
    print(f"\n{label}")
    print(f"  trades {len(trades)} | win rate {len(wins)/len(pnls)*100:.0f}% | "
          f"expectancy {expectancy:+.2f}%/trade | profit factor {pf:.2f} | "
          f"avg win {statistics.mean(wins):+.2f}% avg loss {statistics.mean(losses):+.2f}% | "
          f"seq max DD {mdd:.1f}%" if wins and losses else
          f"  trades {len(trades)} | expectancy {expectancy:+.2f}%")


def main():
    print("Loading SPY regime map...")
    bull = spy_bull_map()
    all_trades = []
    for sym in UNIVERSE:
        try:
            bars = get_bars(sym)
        except Exception as e:  # noqa: BLE001
            print(f"  skip {sym}: {e}")
            continue
        if len(bars) < 60:
            continue
        for variant in ("day", "swing"):
            all_trades += list(simulate(sym, bars, bull, variant))

    for variant in ("day", "swing"):
        subset = [t for t in all_trades if t["variant"] == variant]
        report(subset, f"=== {variant.upper()} variant, all trades (judgment-free floor) ===")
        for reg in ("bull", "bear"):
            report([t for t in subset if t["regime"] == reg], f"  -- {reg} regime only")
        for lo, hi in ((4, 8), (8, 15), (15, 100)):
            report([t for t in subset if lo <= t["gap_pct"] < hi], f"  -- gap {lo}-{hi}%")

    os.makedirs("logs", exist_ok=True)
    with open(RESULTS_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_trades[0].keys()) if all_trades else ["none"])
        w.writeheader()
        for t in all_trades:
            w.writerow(t)
    print(f"\nFull results -> {RESULTS_CSV}")
    print("Reminder: this is the JUDGMENT-FREE floor with pessimistic fills. "
          "Live edge = live expectancy minus this floor.")


if __name__ == "__main__":
    main()
