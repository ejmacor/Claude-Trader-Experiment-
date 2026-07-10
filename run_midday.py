"""
run_midday.py — v2 midday management session (12:30pm ET via GitHub Actions).

The v1 system's biggest execution weakness: after the open it could never
touch a trade again. This session gives every open position exactly one
management decision per day, governed by mechanical rules first and a
Claude judgment call second.

Mechanical rules (always applied, no model involved):
  R1 CUT:     unrealized P&L <= MIDDAY_CUT_THRESHOLD_R (default -0.75R)
              -> thesis is failing before the stop; free the risk budget.
  R2 PROTECT: unrealized P&L >= MIDDAY_TIGHTEN_TRIGGER_R (default +1.5R)
              -> raise stop to breakeven + 0.2R; a winner may not become a loser.
  R3 TIME:    SWING position older than SWING_MAX_HOLD_DAYS -> close (time stop).

Claude judgment (only for positions untouched by R1-R3):
  Sees position, entry thesis, fresh news since entry -> HOLD or CLOSE with
  reasoning. Claude can only close or hold — it can never add or widen stops.

Everything is logged to logs/midday.jsonl for the dashboard/audit trail.
"""

import csv
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import anthropic

import config
import executor
import scanner  # reuse news fetch + retry helper
import trade_logger

MIDDAY_LOG = "logs/midday.jsonl"


def _client():
    """Lazy: a missing ANTHROPIC_API_KEY must never prevent the mechanical
    R1-R3 rules from protecting open positions."""
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

JUDGE_PROMPT = """You are the midday risk manager for a paper-trading experiment.
For each open position you get: entry price, current price, R multiple
(P&L in units of initial stop distance), the original entry reasoning and
exit thesis, and news published since entry.

Decide HOLD or CLOSE for each. Close if the exit thesis has triggered, the
catalyst has been walked back / debunked, or fresh news breaks the original
reasoning. Otherwise hold — the bracket manages the exit. You cannot add,
resize, or move stops.

Respond ONLY with JSON: {"positions": [{"symbol": "X", "action": "HOLD|CLOSE",
"reason": "1 sentence"}]}"""


def entry_context():
    """symbol -> (reasoning, exit_thesis, module, entry_date) from trade log."""
    ctx = {}
    try:
        with open(config.TRADE_LOG_CSV, newline="") as f:
            for r in csv.DictReader(f):
                if r.get("skipped") in ("True", "true"):
                    continue
                ctx[r["symbol"]] = {
                    "reasoning": r.get("reasoning", ""),
                    "exit_thesis": r.get("key_risk", ""),
                    "module": r.get("module", "DAY_MOMENTUM"),
                    "entry_date": r.get("date", ""),
                }
    except FileNotFoundError:
        pass
    return ctx


def stop_distance(symbol, entry_px):
    """Initial risk per share from the open stop order; fallback to ceiling pct."""
    for o in executor.get_open_orders():
        if o["symbol"] == symbol and o.get("side") == "sell" and o.get("type") in ("stop", "stop_limit"):
            sp = float(o.get("stop_price") or 0)
            if 0 < sp < entry_px:
                return entry_px - sp
    return entry_px * config.STOP_PCT_CEIL / 100


def age_in_days(entry_date):
    try:
        d0 = datetime.fromisoformat(entry_date).date()
        return (datetime.now(ZoneInfo("America/New_York")).date() - d0).days
    except Exception:  # noqa: BLE001
        return 0


def main():
    if not config.MIDDAY_ENABLED:
        print("Midday session disabled in config.")
        return

    # Fresh equity snapshot — keeps the dashboard curve tracking the
    # account intraday instead of freezing at the morning print.
    try:
        trade_logger.log_equity(executor.get_account())
    except Exception as e:  # noqa: BLE001 — snapshot failure never blocks management
        print(f"Equity snapshot failed (ignored): {e}")

    positions = executor.get_open_positions()
    if not positions:
        print("No open positions — nothing to manage.")
        return

    ctx = entry_context()

    # Swing detection fallback: any symbol with a live GTC sell bracket is a
    # swing even if the trade log predates the module column.
    gtc_swings = set()
    try:
        for o in executor.get_open_orders():
            if o.get("side") == "sell" and o.get("time_in_force") == "gtc":
                gtc_swings.add(o["symbol"])
    except Exception:  # noqa: BLE001
        pass

    actions, needs_judgment = [], []

    for p in positions:
        sym = p["symbol"]
        entry = float(p["avg_entry_price"])
        current = float(p["current_price"])
        risk = stop_distance(sym, entry)
        r_multiple = (current - entry) / risk if risk else 0.0
        meta = ctx.get(sym, {})
        is_swing = meta.get("module") == "SWING_CATALYST" or sym in gtc_swings
        pos = {"symbol": sym, "entry": entry, "current": current,
               "r_multiple": round(r_multiple, 2), **meta}

        # R3 — swing time stop
        if is_swing and age_in_days(meta.get("entry_date", "")) >= config.SWING_MAX_HOLD_DAYS:
            executor.close_position(sym)
            actions.append({**pos, "action": "CLOSE", "rule": "TIME_STOP",
                            "reason": f"swing hold reached {config.SWING_MAX_HOLD_DAYS} days"})
            continue
        # R1 — cut failing trades before the stop
        if r_multiple <= config.MIDDAY_CUT_THRESHOLD_R:
            executor.close_position(sym)
            actions.append({**pos, "action": "CLOSE", "rule": "MIDDAY_CUT",
                            "reason": f"{r_multiple:.2f}R by midday — thesis failing"})
            continue
        # R2 — protect winners
        if r_multiple >= config.MIDDAY_TIGHTEN_TRIGGER_R:
            new_stop = round(entry + 0.2 * risk, 2)
            executor.replace_stop(sym, new_stop)
            actions.append({**pos, "action": "TIGHTEN", "rule": "PROTECT_WINNER",
                            "reason": f"+{r_multiple:.2f}R — stop raised to {new_stop} (breakeven+)"})
            continue

        # fresh news since entry for Claude's judgment call
        try:
            pos["news_since_entry"] = scanner.get_news(sym, hours_back=6)[:5]
        except Exception:  # noqa: BLE001
            pos["news_since_entry"] = []
        needs_judgment.append(pos)

    # Claude judgment on the remainder
    if needs_judgment:
        try:
            resp = _client().messages.create(
                model=config.CLAUDE_MODEL, max_tokens=1000, system=JUDGE_PROMPT,
                messages=[{"role": "user", "content": json.dumps(needs_judgment, indent=2)}],
            )
            raw = "".join(b.text for b in resp.content if b.type == "text")
            raw = raw.replace("```json", "").replace("```", "").strip()
            verdicts = {v["symbol"]: v for v in json.loads(raw).get("positions", [])}
        except Exception as e:  # noqa: BLE001 — model failure = hold everything
            print(f"Judgment call failed ({e}) — holding all remaining positions.")
            verdicts = {}
        for pos in needs_judgment:
            v = verdicts.get(pos["symbol"], {"action": "HOLD", "reason": "no verdict — default hold"})
            if v.get("action") == "CLOSE":
                try:
                    executor.close_position(pos["symbol"])
                except Exception as e:  # noqa: BLE001
                    v["reason"] += f" (close failed: {e})"
            actions.append({**pos, "action": v.get("action", "HOLD"),
                            "rule": "CLAUDE_JUDGMENT", "reason": v.get("reason", "")})

    os.makedirs("logs", exist_ok=True)
    with open(MIDDAY_LOG, "a") as f:
        f.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                            "actions": actions}) + "\n")

    for a in actions:
        print(f"{a['symbol']:6s} {a['action']:8s} [{a['rule']}] {a['reason']}")
    print(f"\nMidday session complete — {len(actions)} position(s) reviewed.")


if __name__ == "__main__":
    main()
