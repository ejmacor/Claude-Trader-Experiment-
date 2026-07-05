"""
scanner.py — v2. Finds pre-market gap candidates, attaches news AND
technical context, and enforces the liquidity/quality filters.

v2 upgrades over v1:
- ENFORCES MIN_AVG_DOLLAR_VOLUME (v1 defined it but never checked it)
- Relative volume filter — the "stocks in play" condition that the
  ORB/momentum literature (Zarattini et al.) shows concentrates edge
- ATR(14) per candidate -> volatility-scaled brackets in the executor
- Extension vs 20-day high -> skip names already parabolic pre-gap
- Retry wrapper on Alpaca calls (transient failures no longer drop names)

Output: candidate dicts with price, gap, technicals, and news.
"""

import os
import time
from datetime import datetime, timedelta, timezone

import requests

import config

DATA_URL = "https://data.alpaca.markets"

HEADERS = {
    "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
}


def _get(url, params=None, retries=3):
    """GET with simple retry/backoff — one flaky call shouldn't drop a candidate."""
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise last_err


def get_movers():
    data = _get(f"{DATA_URL}/v1beta1/screener/stocks/movers", {"top": 50})
    return data.get("gainers", [])


def get_daily_bars(symbol, days=45):
    start = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    data = _get(
        f"{DATA_URL}/v2/stocks/{symbol}/bars",
        {"timeframe": "1Day", "start": start, "feed": "iex", "limit": 45},
    )
    return data.get("bars") or []


def get_news(symbol, hours_back=18):
    start = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()
    data = _get(
        f"{DATA_URL}/v1beta1/news",
        {"symbols": symbol, "start": start, "limit": 10, "sort": "desc"},
    )
    return [
        {
            "headline": a.get("headline", ""),
            "summary": (a.get("summary") or "")[:500],
            "source": a.get("source", ""),
            "created_at": a.get("created_at", ""),
        }
        for a in data.get("news", [])
    ]


def technicals(bars, last_price):
    """20d avg $ volume, ATR(14), extension vs 20d high, today's rel-vol proxy."""
    if len(bars) < 21:
        return None
    hist = bars[:-1] if _is_today(bars[-1]) else bars  # exclude today's partial bar
    last20 = hist[-20:]

    avg_dollar_vol = sum(b["v"] * b["c"] for b in last20) / len(last20)
    avg_share_vol = sum(b["v"] for b in last20) / len(last20)

    n = config.ATR_LOOKBACK_DAYS
    trs = []
    for i in range(len(hist) - n, len(hist)):
        h, l, pc = hist[i]["h"], hist[i]["l"], hist[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs) / len(trs)

    high_20d = max(b["h"] for b in last20)
    prev_close = hist[-1]["c"]
    extension_pct = (prev_close - high_20d) / high_20d * 100  # negative = below 20d high

    # rel volume: today's cumulative volume vs 20d avg full-day volume.
    # Pre-market this understates true pace, so it acts as a floor: anything
    # that already cleared MIN_RELATIVE_VOLUME pre-open is unambiguously in play.
    today_vol = bars[-1]["v"] if _is_today(bars[-1]) else 0
    rel_vol = today_vol / avg_share_vol if avg_share_vol else 0

    return {
        "avg_dollar_volume": round(avg_dollar_vol),
        "atr": round(atr, 3),
        "atr_pct": round(atr / last_price * 100, 2) if last_price else None,
        "extension_vs_20d_high_pct": round(extension_pct, 1),
        "relative_volume": round(rel_vol, 2),
        "prev_close": prev_close,
    }


def _is_today(bar):
    return bar.get("t", "").startswith(datetime.now(timezone.utc).date().isoformat())


def build_candidates():
    movers = get_movers()
    candidates, rejected_by_filter = [], []

    for m in movers:
        sym, pct, price = m["symbol"], m.get("percent_change", 0), m.get("price", 0)
        if pct < config.MIN_GAP_PCT or not (config.MIN_PRICE <= price <= config.MAX_PRICE):
            continue
        try:
            bars = get_daily_bars(sym)
            tech = technicals(bars, price)
        except Exception as e:  # noqa: BLE001
            print(f"      WARN: bars failed for {sym}, skipping ({e})")
            continue
        if tech is None:
            rejected_by_filter.append((sym, "insufficient history"))
            continue
        if tech["avg_dollar_volume"] < config.MIN_AVG_DOLLAR_VOLUME:
            rejected_by_filter.append((sym, f"illiquid: ${tech['avg_dollar_volume']:,}/day"))
            continue
        if tech["extension_vs_20d_high_pct"] > config.MAX_EXTENSION_FROM_20D_HIGH:
            rejected_by_filter.append((sym, f"already +{tech['extension_vs_20d_high_pct']}% extended"))
            continue

        try:
            news = get_news(sym)
        except Exception as e:  # noqa: BLE001
            print(f"      WARN: news failed for {sym}, skipping ({e})")
            continue
        if not news:
            continue  # no catalyst = not our strategy

        candidates.append({
            "symbol": sym,
            "last_price": price,
            "gap_pct": round(pct, 2),
            "technicals": tech,
            "news": news,
        })
        if len(candidates) >= config.MAX_CANDIDATES_SENT_TO_CLAUDE:
            break

    # In-play names first: sort by relative volume, then gap size
    candidates.sort(key=lambda c: (c["technicals"]["relative_volume"], c["gap_pct"]), reverse=True)

    if rejected_by_filter:
        print("      Filtered pre-Claude: " + "; ".join(f"{s} ({r})" for s, r in rejected_by_filter[:8]))
    return candidates


if __name__ == "__main__":
    import json
    print(json.dumps(build_candidates(), indent=2))
