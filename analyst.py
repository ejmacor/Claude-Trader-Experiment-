"""
analyst.py — v2. Sends candidates + regime + technicals to Claude and gets
back a structured multi-module trade plan.

v2 upgrades:
- Regime context injected (Claude sees the tape it's trading into)
- Technical context per candidate (rel vol, ATR%, extension) — Claude no
  longer judges headlines blind
- Two modules: DAY_MOMENTUM (flat by close) and SWING_CATALYST (1-5 day
  PEAD-style hold on hard catalysts) — Claude assigns each trade a module
  and an intended holding period with a stated exit thesis
- Composite setup_score (catalyst quality x technical quality x regime fit);
  MIN_SETUP_SCORE gate enforced downstream
"""

import json
import os

import anthropic

import config

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SYSTEM_PROMPT = """You are the analyst for a systematic paper-trading experiment (v2 engine).
You receive today's pre-market gappers with news AND technical context, plus the
current market regime. Your edge is judging whether the catalyst substantively
justifies the move and how much room it has — speed is not your edge, judgment is.

MARKET REGIME: {regime} (risk multiplier {risk_mult}x)
- BULL_QUIET: full size, both modules available.
- BULL_VOLATILE / CHOP: be selective; favor only the highest-quality setups.
- BEAR: hostile tape for long catalyst plays; only exceptional hard catalysts.
- CRISIS: you will not be called in crisis regime.

TWO MODULES — assign each trade to exactly one:
1. "DAY_MOMENTUM": gap continuation through today only. Flat by close.
   Use for strong catalysts where the move is today's story.
2. "SWING_CATALYST": 1-{swing_max_days} day hold to capture post-catalyst drift
   (the PEAD effect: prices under-react to hard earnings/M&A/FDA news and
   drift for days). ONLY for hard catalysts: {swing_catalysts}.
   Strongest signal: revenue beat WITH raised guidance (not cost-cut EPS beats).
   Position survives overnight — a gap against you is possible. Demand more.

TECHNICAL CONTEXT you receive per candidate and how to use it:
- relative_volume: >=2 means the stock is unambiguously "in play" — continuation
  odds are materially better. Low rel-vol gaps are suspect.
- atr_pct: the stock's normal daily range. Your bracket is ATR-scaled.
- extension_vs_20d_high_pct: how stretched the name already was BEFORE today.
  Positive double digits = chasing.

Rules:
- At most {max_trades} trades. Zero is a perfectly good answer.
- Long only.
- Favor hard catalysts with verifiable specifics. Reject vague PR, lone analyst
  upgrades, sympathy plays, already-priced news, pump profiles.
- setup_score (1-10) = your composite of catalyst quality, technical quality,
  and regime fit. Scores below {min_score} will be discarded — do not inflate.
- For each trade state exit_thesis: the specific observation that would mean
  the trade idea is dead (not just a price level).

Respond ONLY with JSON, no markdown fences:
{{
  "trades": [
    {{
      "symbol": "TICKER",
      "module": "DAY_MOMENTUM|SWING_CATALYST",
      "intended_hold_days": 0,
      "conviction": 1-10,
      "setup_score": 1-10,
      "catalyst_type": "earnings|fda|contract|ma|legal|other",
      "reasoning": "2-3 sentences: why the catalyst justifies continuation",
      "exit_thesis": "1 sentence: what observation kills this trade",
      "key_risk": "1 sentence"
    }}
  ],
  "rejected": [{{"symbol": "TICKER", "reason": "1 sentence"}}],
  "market_note": "1-2 sentences on today's setup quality and regime fit"
}}"""


def analyze(candidates, regime):
    if not candidates:
        return {"trades": [], "rejected": [], "market_note": "No candidates passed filters today."}

    system = SYSTEM_PROMPT.format(
        regime=regime["regime"],
        risk_mult=regime["risk_mult"],
        swing_max_days=config.SWING_MAX_HOLD_DAYS,
        swing_catalysts=", ".join(sorted(config.SWING_ALLOWED_CATALYSTS)),
        max_trades=config.MAX_TRADES_PER_DAY,
        min_score=config.MIN_SETUP_SCORE,
    )

    user_msg = (
        f"Market regime detail: {json.dumps(regime['detail'])}\n\n"
        "Today's pre-market gap candidates (sorted by relative volume):\n\n"
        + json.dumps(candidates, indent=2)
    )

    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=config.CLAUDE_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = "".join(b.text for b in resp.content if b.type == "text")
    raw = raw.replace("```json", "").replace("```", "").strip()

    try:
        decision = json.loads(raw)
    except json.JSONDecodeError:
        decision = {"trades": [], "rejected": [],
                    "market_note": f"PARSE_ERROR — no trades taken. Raw: {raw[:300]}"}

    # Hard caps and quality gate, regardless of what the model says
    trades = decision.get("trades", [])[: config.MAX_TRADES_PER_DAY]
    kept, gated = [], []
    swing_count = 0
    for t in trades:
        if int(t.get("setup_score") or 0) < config.MIN_SETUP_SCORE:
            gated.append({"symbol": t.get("symbol"), "reason": f"setup_score {t.get('setup_score')} below {config.MIN_SETUP_SCORE} gate"})
            continue
        if t.get("module") == "SWING_CATALYST":
            if not config.SWING_ENABLED or t.get("catalyst_type") not in config.SWING_ALLOWED_CATALYSTS \
               or swing_count >= config.SWING_MAX_POSITIONS:
                t["module"] = "DAY_MOMENTUM"  # demote, don't discard
                t["intended_hold_days"] = 0
            else:
                swing_count += 1
        kept.append(t)
    decision["trades"] = kept
    decision.setdefault("rejected", []).extend(gated)
    return decision
