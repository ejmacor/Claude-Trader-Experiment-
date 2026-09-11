"""
scanner.py — v2. Finds pre-market gap candidates, attaches news AND
technical context, and enforces the liquidity/quality filters.

v2 upgrades over v1:
- ENFORCES MIN_AVG_DOLLAR_VOLUME (v1 defined it but never checked it)
- Relative volume filter — the "stocks in play" condition that the
  ORB/momentum literature (Zarattini et al.) shows concentrates edge.
  NOW ACTUALLY ENFORCED (2026-09-11): v2.0-2.1 computed rel_vol and passed
  it to Claude as context but never filtered on it — the same class of bug
  as v1's unused MIN_AVG_DOLLAR_VOLUME. Enforcement is pace-adjusted so a
  pre-open run doesn't compare a nearly-empty daily bar against a full-day
  average (see expected_volume_fraction), measured on true live volume
  summed from 1-minute bars (daily-bar premarket coverage is unreliable).
- ATR(14) per candidate -> volatility-scaled brackets in the executor
- Extension vs 20-day high -> skip names already parabolic pre-gap
- Retry wrapper on Alpaca calls (transient failures no longer drop names)

Output: candidate dicts with price, gap, technicals, and news.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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


def get_today_volume(symbol):
    """Today's actual traded share volume so far, summed from 1-minute bars.

    The daily bar's coverage of the premarket session is feed/condition
    dependent, which is exactly what you don't want a hard filter to rest on
    at 8:10am. IEX minute bars include extended-hours trades, so summing them
    gives a true "volume so far today" at ANY time of day — pre-open
    included. Returns 0 for a symbol with no trades yet today.
    """
    start = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    data = _get(
        f"{DATA_URL}/v2/stocks/{symbol}/bars",
        {"timeframe": "1Min", "start": start, "feed": "iex", "limit": 1000},
    )
    return sum(b.get("v", 0) for b in (data.get("bars") or []))


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


# Cumulative fraction of a typical session's volume completed by a given
# ET wall-clock minute. Intraday volume is U-shaped (heavy open, quiet lunch,
# heavy close); these knots are a standard approximation.
#   (minute-of-day ET, cumulative fraction)
_VOLUME_CURVE = [
    (0,    0.03),   # overnight/premarket floor
    (570,  0.03),   # 9:30  open
    (600,  0.18),   # 10:00 - first half hour is the heaviest
    (690,  0.45),   # 11:30
    (870,  0.72),   # 14:30
    (960,  1.00),   # 16:00 close
    (1440, 1.00),
]


def expected_volume_fraction(now_et=None):
    """Fraction of a normal day's share volume typically done by `now_et`.

    Used to pace-adjust the MIN_RELATIVE_VOLUME floor: a name that is
    genuinely "in play" should be running at >= 1.5x the NORMAL pace for
    this time of day, not 1.5x a full day's volume before lunch.
    Piecewise-linear between the knots above.
    """
    if now_et is None:
        now_et = datetime.now(ZoneInfo("America/New_York"))
    t = now_et.hour * 60 + now_et.minute
    curve = _VOLUME_CURVE
    if t <= curve[0][0]:
        return curve[0][1]
    for (t0, f0), (t1, f1) in zip(curve, curve[1:]):
        if t0 <= t <= t1:
            if t1 == t0:
                return f1
            return f0 + (f1 - f0) * (t - t0) / (t1 - t0)
    return curve[-1][1]


def rel_vol_floor(now_et=None):
    """The raw (full-day-basis) relative volume a candidate must already show
    to be trading at MIN_RELATIVE_VOLUME x the normal pace RIGHT NOW."""
    return config.MIN_RELATIVE_VOLUME * expected_volume_fraction(now_et)


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
        "avg_share_volume": round(avg_share_vol),
        "atr": round(atr, 3),
        "atr_pct": round(atr / last_price * 100, 2) if last_price else None,
        "extension_vs_20d_high_pct": round(extension_pct, 1),
        "relative_volume": round(rel_vol, 2),
        "prev_close": prev_close,
    }


def _is_today(bar):
    """Bar 't' is UTC ISO; the trading day is ET. Compare on the ET date."""
    ts = bar.get("t", "")
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(
            ZoneInfo("America/New_York")).date().isoformat()
    except (ValueError, TypeError):
        return False
    return d == datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def build_candidates():
    movers = get_movers()
    candidates, rejected_by_filter = [], []

    # ---- Funnel instrumentation (observability only — no filter changes) ----
    funnel = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "movers_returned": len(movers),
        "top_movers_raw": [
            {"symbol": m.get("symbol"), "pct": round(m.get("percent_change", 0), 2),
             "price": m.get("price", 0)}
            for m in movers[:8]
        ],
        "passed_gap_and_price": 0,
        "failed_bars_fetch": 0,
        "rejected_insufficient_history": 0,
        "rejected_illiquid": 0,
        "rejected_low_rel_vol": 0,
        "rejected_extended": 0,
        "failed_news_fetch": 0,
        "rejected_no_news": 0,
        "final_candidates": 0,
    }

    for m in movers:
        sym, pct, price = m["symbol"], m.get("percent_change", 0), m.get("price", 0)
        if pct < config.MIN_GAP_PCT or not (config.MIN_PRICE <= price <= config.MAX_PRICE):
            continue
        funnel["passed_gap_and_price"] += 1
        try:
            bars = get_daily_bars(sym)
            tech = technicals(bars, price)
        except Exception as e:  # noqa: BLE001
            print(f"      WARN: bars failed for {sym}, skipping ({e})")
            funnel["failed_bars_fetch"] += 1
            continue
        if tech is None:
            rejected_by_filter.append((sym, "insufficient history"))
            funnel["rejected_insufficient_history"] += 1
            continue
        if tech["avg_dollar_volume"] < config.MIN_AVG_DOLLAR_VOLUME:
            rejected_by_filter.append((sym, f"illiquid: ${tech['avg_dollar_volume']:,}/day"))
            funnel["rejected_illiquid"] += 1
            continue
        if tech["extension_vs_20d_high_pct"] > config.MAX_EXTENSION_FROM_20D_HIGH:
            rejected_by_filter.append((sym, f"already +{tech['extension_vs_20d_high_pct']}% extended"))
            funnel["rejected_extended"] += 1
            continue

        # Hard "stocks in play" floor, pace-adjusted (2026-09-11).
        # MIN_RELATIVE_VOLUME existed in config since v2.0 but was never
        # checked, so the engine took entries on names with dead tape —
        # TWLO entered at rel vol 0.06, a loss. A raw full-day comparison
        # would be wrong too: hours before the close the day is only partly
        # done, so the floor scales with the time of day via
        # expected_volume_fraction(). Measured against a true live volume
        # from minute bars (daily-bar premarket coverage is unreliable);
        # falls back to the daily-bar proxy if the minute fetch fails.
        try:
            live_vol = get_today_volume(sym)
            rel_now = (live_vol / tech["avg_share_volume"]
                       if tech["avg_share_volume"] else 0)
        except Exception as e:  # noqa: BLE001
            print(f"      WARN: live volume failed for {sym}, "
                  f"using daily-bar proxy ({e})")
            rel_now = tech["relative_volume"]
        tech["relative_volume_live"] = round(rel_now, 2)
        floor_now = rel_vol_floor()
        if rel_now < floor_now:
            rejected_by_filter.append(
                (sym, f"low rel vol {rel_now:.2f} "
                      f"(pace floor {floor_now:.2f} this time of day)"))
            funnel["rejected_low_rel_vol"] += 1
            continue

        try:
            news = get_news(sym)
        except Exception as e:  # noqa: BLE001
            print(f"      WARN: news failed for {sym}, skipping ({e})")
            funnel["failed_news_fetch"] += 1
            continue
        if not news:
            rejected_by_filter.append((sym, "no news catalyst"))
            funnel["rejected_no_news"] += 1
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

    # ---- Write funnel record ----
    funnel["final_candidates"] = len(candidates)
    funnel["rejects_detail"] = [f"{s}: {r}" for s, r in rejected_by_filter[:12]]
    try:
        import json as _json
        os.makedirs("logs", exist_ok=True)
        with open("logs/funnel.jsonl", "a") as f:
            f.write(_json.dumps(funnel) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"      WARN: funnel log write failed ({e})")
    print(
        f"      FUNNEL: movers={funnel['movers_returned']} -> "
        f"gap/price={funnel['passed_gap_and_price']} -> "
        f"liquid={funnel['passed_gap_and_price'] - funnel['rejected_insufficient_history'] - funnel['rejected_illiquid'] - funnel['failed_bars_fetch']} -> "
        f"not-extended={funnel['passed_gap_and_price'] - funnel['rejected_insufficient_history'] - funnel['rejected_illiquid'] - funnel['failed_bars_fetch'] - funnel['rejected_extended']} -> "
        f"in-play={funnel['passed_gap_and_price'] - funnel['rejected_insufficient_history'] - funnel['rejected_illiquid'] - funnel['failed_bars_fetch'] - funnel['rejected_extended'] - funnel['rejected_low_rel_vol']} -> "
        f"with-news={funnel['final_candidates']}"
    )
    return candidates


if __name__ == "__main__":
    import json
    print(json.dumps(build_candidates(), indent=2))
