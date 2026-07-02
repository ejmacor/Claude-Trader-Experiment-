"""
scanner.py — Finds pre-market gap candidates and attaches their news.

Uses Alpaca's free market data (IEX feed) and news API.
Output: a list of candidate dicts ready to send to Claude.
"""

import os
from datetime import datetime, timedelta, timezone

import requests

import config

ALPACA_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET = os.environ["ALPACA_SECRET_KEY"]
DATA_URL = "https://data.alpaca.markets"

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}


def get_most_active_and_movers():
    """Pull Alpaca's top market movers (gainers) snapshot."""
    url = f"{DATA_URL}/v1beta1/screener/stocks/movers"
    resp = requests.get(url, headers=HEADERS, params={"top": 50}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("gainers", [])


def get_news(symbol, hours_back=18):
    """Recent news headlines + summaries for a symbol."""
    start = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()
    url = f"{DATA_URL}/v1beta1/news"
    resp = requests.get(
        url, headers=HEADERS,
        params={"symbols": symbol, "start": start, "limit": 10, "sort": "desc"},
        timeout=30,
    )
    resp.raise_for_status()
    articles = resp.json().get("news", [])
    return [
        {
            "headline": a.get("headline", ""),
            "summary": (a.get("summary") or "")[:500],
            "source": a.get("source", ""),
            "created_at": a.get("created_at", ""),
        }
        for a in articles
    ]


def build_candidates():
    """Full pipeline: movers -> filters -> news attach -> candidate list."""
    movers = get_most_active_and_movers()

    filtered = []
    for m in movers:
        pct = m.get("percent_change", 0)
        price = m.get("price", 0)
        if pct < config.MIN_GAP_PCT:
            continue
        if not (config.MIN_PRICE <= price <= config.MAX_PRICE):
            continue
        filtered.append(m)

    filtered = filtered[: config.MAX_CANDIDATES_SENT_TO_CLAUDE]

    candidates = []
    for m in filtered:
        sym = m["symbol"]
        try:
            news = get_news(sym)
        except Exception as e:  # noqa: BLE001
            # One flaky news call shouldn't kill the whole morning.
            # Skip this ticker (fail-safe: fewer candidates, never bad data).
            print(f"      WARN: news lookup failed for {sym}, skipping ({e})")
            continue
        if not news:
            # No news = no catalyst = not our strategy. Skip.
            continue
        candidates.append(
            {
                "symbol": sym,
                "last_price": m.get("price"),
                "gap_pct": round(m.get("percent_change", 0), 2),
                "news": news,
            }
        )

    return candidates


if __name__ == "__main__":
    import json
    print(json.dumps(build_candidates(), indent=2))
