"""
analyst.py — Sends the morning's candidates to Claude and gets back
a structured trade plan (or NO_TRADE) for each.

The prompt is part of the frozen strategy. Don't tweak it mid-experiment.
"""

import json
import os

import anthropic

import config

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SYSTEM_PROMPT = """You are the analyst for a systematic paper-trading experiment.
Strategy: pre-market catalyst interpretation. You receive today's pre-market
gappers with their news. Your ONLY edge is judging whether the news catalyst
substantively justifies the gap and has room to continue through the day.

Rules you must follow:
- Recommend AT MOST {max_trades} trades. Zero is a perfectly good answer.
- Only recommend LONG entries (this experiment is long-only).
- Favor: hard catalysts (earnings beats WITH raised guidance, FDA approvals,
  signed contracts with dollar figures, M&A). Substance over headline sizzle.
- Reject: vague PR, analyst upgrades alone, sympathy plays, gaps with no
  clear catalyst, catalysts already fully priced (e.g., rumor confirmed).
- Reject anything that smells like a pump: low-quality sources, promotional
  language, no verifiable specifics.
- You cannot watch the market intraday. Entries are at the open with a fixed
  {stop}% stop and {target}% target bracket. Ask yourself: "would I take this
  trade knowing I can't touch it again?"

Respond with ONLY a JSON object, no markdown fences, no preamble:
{{
  "trades": [
    {{
      "symbol": "TICKER",
      "conviction": 1-10,
      "catalyst_type": "earnings|fda|contract|ma|legal|other",
      "reasoning": "2-3 sentences on why the catalyst justifies continuation",
      "key_risk": "1 sentence on what kills this trade"
    }}
  ],
  "rejected": [
    {{"symbol": "TICKER", "reason": "1 sentence"}}
  ],
  "market_note": "1-2 sentences on overall quality of today's setups"
}}
If nothing qualifies, return "trades": [] and explain in market_note."""


def analyze(candidates):
    """Send candidates to Claude, return parsed decision dict."""
    if not candidates:
        return {"trades": [], "rejected": [], "market_note": "No candidates passed filters today."}

    system = SYSTEM_PROMPT.format(
        max_trades=config.MAX_TRADES_PER_DAY,
        stop=config.STOP_LOSS_PCT,
        target=config.TAKE_PROFIT_PCT,
    )

    user_msg = (
        "Here are today's pre-market gap candidates with their news:\n\n"
        + json.dumps(candidates, indent=2)
    )

    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=config.CLAUDE_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = "".join(block.text for block in resp.content if block.type == "text")
    raw = raw.replace("```json", "").replace("```", "").strip()

    try:
        decision = json.loads(raw)
    except json.JSONDecodeError:
        # Fail SAFE: if the model output can't be parsed, trade nothing.
        decision = {
            "trades": [],
            "rejected": [],
            "market_note": f"PARSE_ERROR — raw output logged, no trades taken. Raw: {raw[:300]}",
        }

    # Hard cap regardless of what the model says
    decision["trades"] = decision.get("trades", [])[: config.MAX_TRADES_PER_DAY]
    return decision
