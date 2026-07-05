"""
regime.py — Market regime classifier. Runs before any trading decision.

Classifies the tape from SPY daily bars (free IEX feed):
  trend: close vs 200-SMA and 50-SMA
  vol:   20-day realized volatility, annualized

-> one of: BULL_QUIET / BULL_VOLATILE / CHOP / BEAR / CRISIS

Why: momentum/breakout strategies demonstrably outperform in trending
regimes and bleed in chop/bear tape; a 200-SMA filter alone removes most
bear-market damage. The regime sets a risk multiplier (config.REGIME_RISK_MULT)
applied to position sizing, and CRISIS blocks new entries entirely.

Output: dict + one JSONL row per run in logs/regime.jsonl.
Fail-safe: any data problem returns CHOP (0.5x sizing), never crashes the run.
"""

import json
import math
import os
from datetime import datetime, timedelta, timezone

import requests

import config

DATA_URL = "https://data.alpaca.markets"


def _headers():
    return {
        "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
    }


def get_spy_daily_closes(days=320):
    start = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    resp = requests.get(
        f"{DATA_URL}/v2/stocks/SPY/bars",
        headers=_headers(),
        params={"timeframe": "1Day", "start": start, "feed": "iex", "limit": 400},
        timeout=30,
    )
    resp.raise_for_status()
    bars = resp.json().get("bars") or []
    return [float(b["c"]) for b in bars]


def realized_vol_annualized(closes, lookback=20):
    """Annualized std-dev of daily log returns over the lookback, in %."""
    if len(closes) < lookback + 1:
        return None
    tail = closes[-(lookback + 1):]
    rets = [math.log(tail[i] / tail[i - 1]) for i in range(1, len(tail))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252) * 100


def classify():
    """Returns {"regime", "risk_mult", "detail"} — never raises."""
    try:
        closes = get_spy_daily_closes()
        if len(closes) < 210:
            raise ValueError(f"only {len(closes)} SPY bars available")

        price = closes[-1]
        sma200 = sum(closes[-200:]) / 200
        sma50 = sum(closes[-50:]) / 50
        vol = realized_vol_annualized(closes)

        if vol is not None and vol >= config.VOL_CRISIS_MIN:
            regime = "CRISIS"
        elif price < sma200:
            regime = "BEAR"
        elif price >= sma200 and price >= sma50:
            regime = "BULL_QUIET" if (vol or 99) < config.VOL_QUIET_MAX else "BULL_VOLATILE"
        else:  # above 200 but below 50 — trend disagreement
            regime = "CHOP"

        detail = {
            "spy": round(price, 2),
            "sma50": round(sma50, 2),
            "sma200": round(sma200, 2),
            "realized_vol_20d_pct": round(vol, 1) if vol else None,
        }
    except Exception as e:  # noqa: BLE001 — fail safe, never block the run
        regime, detail = "CHOP", {"error": str(e), "note": "data failure -> defaulted to CHOP (0.5x risk)"}

    result = {
        "regime": regime,
        "risk_mult": config.REGIME_RISK_MULT.get(regime, 0.5),
        "detail": detail,
    }
    _log(result)
    return result


def _log(result):
    try:
        os.makedirs("logs", exist_ok=True)
        row = {"timestamp": datetime.now(timezone.utc).isoformat(), **result}
        with open(config.REGIME_LOG_JSONL, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    print(json.dumps(classify(), indent=2))
