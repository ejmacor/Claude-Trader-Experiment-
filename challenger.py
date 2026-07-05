"""
challenger.py — SHADOW-MODE bear-case reviewer. The second brain.

After the analyst (v1) proposes trades, a second Claude call reviews each
one as an independent risk officer whose job is to find reasons NOT to
trade. Verdicts are logged, never enforced. Combined with shadow_gate,
this forms the hypothetical "v2" pipeline:

    v2 takes a trade only if v1 proposed it
    AND the gate would not veto it
    AND the challenger confirms it.

The dashboard replays v1's actual fills to chart what v2 would have
earned. Cost: one extra Sonnet call per morning (~a cent).
"""

import json
import os
from datetime import datetime, timezone

import anthropic

import config

CHALLENGER_LOG = "logs/challenger.jsonl"

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SYSTEM_PROMPT = """You are the independent risk officer for a systematic
paper-trading experiment. An analyst has proposed long day-trades on
pre-market gappers. Your ONLY job is to stress-test each proposal and
find the reasons it fails. You are rewarded for catching bad trades,
not for agreeing.

For each proposed trade, interrogate:
- Is the catalyst already fully priced into the gap?
- Is the gap so extended that the reward/risk at the open is poor?
- Does the sourcing smell promotional or thin?
- What does the analyst's own stated reasoning gloss over?
- Would YOU take this knowing entries are at the open with a fixed
  {stop}% stop / {target}% target and no intraday management?

Be adversarial but honest: if a trade genuinely holds up under attack,
confirm it. Rejecting everything is as useless as confirming everything.

Respond with ONLY a JSON object, no markdown fences, no preamble:
{{
  "reviews": [
    {{
      "symbol": "TICKER",
      "verdict": "CONFIRM|REJECT",
      "bear_case": "2-3 sentences: the strongest case against this trade",
      "confidence": 1-10
    }}
  ],
  "note": "1 sentence on the overall quality of the analyst's slate"
}}"""


def review(candidates, decision):
    """Challenge each proposed trade. Returns the log row (also on disk).

    Fail-safe: any API/parse error yields verdict ERROR per trade, which
    the dashboard treats as CONFIRM — infrastructure noise must never
    fake alpha for the shadow v2.
    """
    trades = decision.get("trades", []) or []
    ts = datetime.now(timezone.utc).isoformat()

    if not trades:
        row = {"timestamp": ts, "mode": "shadow", "reviews": [],
               "note": "no trades proposed — nothing to challenge"}
        _append(row)
        return row

    by_symbol = {c["symbol"]: c for c in candidates}
    payload = {
        "proposed_trades": trades,
        "candidates": [by_symbol[t["symbol"]] for t in trades if t.get("symbol") in by_symbol],
    }

    try:
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1500,
            system=SYSTEM_PROMPT.format(stop=config.STOP_PCT_CEIL, target=config.STOP_PCT_CEIL * config.TARGET_ATR_MULT / config.STOP_ATR_MULT),
            messages=[{"role": "user", "content":
                       "Challenge these proposed trades:\n\n" + json.dumps(payload, indent=2)}],
        )
        raw = "".join(b.text for b in resp.content if b.type == "text")
        raw = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)
        reviews = parsed.get("reviews", [])
        note = parsed.get("note", "")
        # Any proposed symbol the model skipped gets ERROR (treated as confirm)
        reviewed = {r.get("symbol") for r in reviews}
        for t in trades:
            if t.get("symbol") not in reviewed:
                reviews.append({"symbol": t.get("symbol"), "verdict": "ERROR",
                                "bear_case": "challenger omitted this symbol", "confidence": 0})
    except Exception as e:  # noqa: BLE001
        reviews = [{"symbol": t.get("symbol"), "verdict": "ERROR",
                    "bear_case": f"challenger failed: {e}", "confidence": 0} for t in trades]
        note = "CHALLENGER_ERROR — all verdicts neutral"

    row = {"timestamp": ts, "mode": "shadow", "reviews": reviews, "note": note}
    _append(row)
    return row


def _append(row):
    os.makedirs("logs", exist_ok=True)
    with open(CHALLENGER_LOG, "a") as f:
        f.write(json.dumps(row) + "\n")
