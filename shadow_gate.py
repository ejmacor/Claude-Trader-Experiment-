"""
shadow_gate.py — SHADOW-MODE risk gate. Observes, never blocks.

Runs after Claude's decision each morning and evaluates every proposed
trade against deterministic risk rules (Bridgewater-style checks scaled
to this strategy). It logs what it WOULD have vetoed or flagged, then
gets out of the way. The live pipeline is untouched.

Why deterministic (no second Claude call): free, instant, zero variance,
and fully auditable. Every flag traces to a hard rule below.

Promotion criteria (decide at day-45 review, ~Aug 16):
  - If vetoed trades underperform allowed trades by a clear margin
    (e.g. avg realized P&L gap > 2pts across 10+ evaluated trades),
    promote the gate to blocking mode as a LOGGED config change.
  - Otherwise it stays shadow through day 91 and informs v2.

Output: logs/shadow_gate.jsonl — one row per morning run.
"""

import csv
import json
import os
from datetime import datetime, timezone

import config

SHADOW_LOG = "logs/shadow_gate.jsonl"

# ---- Shadow rule thresholds (tune in v2, not mid-run) ----
EXTENDED_GAP_PCT = 30.0      # chasing a >30% gap: continuation odds decay
LOW_CONVICTION_VETO = 5      # conviction <= 5: why are we taking this?
LOW_CONVICTION_WARN = 6      # conviction == 6: borderline
PUMP_PRICE = 10.00           # sub-$10 stock...
PUMP_GAP_PCT = 20.0          # ...gapping 20%+ fits the pump profile
MIN_NEWS_SOURCES = 2         # single-source catalysts are fragile
DAY_HEAT_WARN_PCT = 2.5      # total % of equity at risk across today's trades
LOSS_STREAK_LOOKBACK = 2     # same catalyst_type stopped out N times running


def _recent_catalyst_losses():
    """Map catalyst_type -> consecutive recent losses, from outcomes.csv.

    Reads TRADED rows newest-last, counts trailing loss streaks per type.
    Fail-safe: any problem reading the file returns {} (rule stays quiet).
    """
    path = "logs/outcomes.csv"
    streaks = {}
    try:
        with open(path, newline="") as f:
            rows = [r for r in csv.DictReader(f) if r.get("action") == "TAKEN"]
        # walk newest -> oldest per catalyst_type until a non-loss breaks it
        for r in reversed(rows):
            ct = (r.get("catalyst_type") or "").strip()
            if not ct or streaks.get(ct) == "broken":
                continue
            try:
                pnl = float(r.get("realized_pnl_pct") or "nan")
            except ValueError:
                continue
            if pnl < 0:
                streaks[ct] = streaks.get(ct, 0) + 1
            else:
                streaks[ct] = "broken"
        return {k: v for k, v in streaks.items() if isinstance(v, int)}
    except Exception:  # noqa: BLE001
        return {}


def evaluate(candidates, decision, open_position_count=0):
    """Evaluate proposed trades. Returns the log row (also appended to disk).

    Never raises in normal operation; caller should still wrap in
    try/except so shadow problems can NEVER touch the live run.
    """
    by_symbol = {c["symbol"]: c for c in candidates}
    trades = decision.get("trades", []) or []
    loss_streaks = _recent_catalyst_losses()

    # Portfolio-level context
    type_counts = {}
    for t in trades:
        ct = t.get("catalyst_type", "other")
        type_counts[ct] = type_counts.get(ct, 0) + 1
    day_heat = len(trades) * config.RISK_PER_TRADE_PCT

    evaluated = []
    for t in trades:
        sym = t.get("symbol", "")
        cand = by_symbol.get(sym, {})
        gap = float(cand.get("gap_pct") or 0)
        price = float(cand.get("last_price") or 0)
        conviction = int(t.get("conviction") or 0)
        ct = t.get("catalyst_type", "other")
        sources = {n.get("source", "") for n in cand.get("news", []) if n.get("source")}

        flags = []

        # R1 — extended gap
        if gap >= EXTENDED_GAP_PCT:
            flags.append({"rule": "EXTENDED_GAP", "level": "veto",
                          "detail": f"+{gap:.1f}% gap — chasing extension past {EXTENDED_GAP_PCT:.0f}%"})

        # R2 — conviction floor
        if conviction <= LOW_CONVICTION_VETO:
            flags.append({"rule": "LOW_CONVICTION", "level": "veto",
                          "detail": f"conviction {conviction}/10 at or below veto floor"})
        elif conviction == LOW_CONVICTION_WARN:
            flags.append({"rule": "LOW_CONVICTION", "level": "warn",
                          "detail": f"conviction {conviction}/10 — borderline"})

        # R3 — pump profile
        if price < PUMP_PRICE and gap >= PUMP_GAP_PCT:
            flags.append({"rule": "PUMP_PROFILE", "level": "veto",
                          "detail": f"${price:.2f} stock gapping +{gap:.1f}% fits pump profile"})

        # R4 — single-source catalyst
        if len(sources) < MIN_NEWS_SOURCES:
            flags.append({"rule": "SINGLE_SOURCE", "level": "warn",
                          "detail": f"{len(sources)} news source(s) — catalyst unverified elsewhere"})

        # R5 — repeating a losing pattern
        streak = loss_streaks.get(ct, 0)
        if streak >= LOSS_STREAK_LOOKBACK:
            flags.append({"rule": "LOSING_PATTERN", "level": "warn",
                          "detail": f"last {streak} '{ct}' trades were losses"})

        # R6 — catalyst concentration across today's slate
        if type_counts.get(ct, 0) >= 2:
            flags.append({"rule": "CATALYST_CONCENTRATION", "level": "warn",
                          "detail": f"{type_counts[ct]} of today's trades share '{ct}' catalyst risk"})

        # R7 — combined daily heat
        if day_heat > DAY_HEAT_WARN_PCT:
            flags.append({"rule": "DAY_HEAT", "level": "warn",
                          "detail": f"{day_heat:.1f}% of equity at risk across today's slate"})

        # R8 — exposure stack (executor guards hard cap; gate notes the squeeze)
        if open_position_count + len(trades) > config.MAX_OPEN_POSITIONS:
            flags.append({"rule": "EXPOSURE_STACK", "level": "warn",
                          "detail": f"{open_position_count} open + {len(trades)} proposed exceeds cap of {config.MAX_OPEN_POSITIONS}"})

        verdict = ("WOULD_VETO" if any(f["level"] == "veto" for f in flags)
                   else "FLAGGED" if flags else "WOULD_ALLOW")
        evaluated.append({"symbol": sym, "conviction": conviction,
                          "catalyst_type": ct, "gap_pct": gap,
                          "verdict": verdict, "flags": flags})

    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "shadow",
        "evaluated": evaluated,
        "summary": {
            "proposed": len(trades),
            "would_allow": sum(1 for e in evaluated if e["verdict"] == "WOULD_ALLOW"),
            "flagged": sum(1 for e in evaluated if e["verdict"] == "FLAGGED"),
            "would_veto": sum(1 for e in evaluated if e["verdict"] == "WOULD_VETO"),
        },
        "note": "no trades proposed — gate idle" if not trades else "",
    }

    os.makedirs("logs", exist_ok=True)
    with open(SHADOW_LOG, "a") as f:
        f.write(json.dumps(row) + "\n")

    return row
