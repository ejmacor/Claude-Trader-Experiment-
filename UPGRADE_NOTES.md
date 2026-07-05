# Claude Trader v2 — Engine Rebuild (2026-07-05)

The v1 frozen run is retired (config preserved at `config_v1_frozen.py.bak`). The v2 clock starts **2026-07-06** — a fresh 90 days on the upgraded engine.

## Audit findings in v1 (what was actually wrong)

1. **`MIN_AVG_DOLLAR_VOLUME` was defined in config but never checked by the scanner.** Illiquid names could reach Claude and get paper fills that would never survive real spreads.
2. **No relative volume filter.** The "stocks in play" condition (rel-vol ≥ ~1.5–2x) is the single most evidence-backed enhancer for gap/breakout continuation (Zarattini, Barbon & Aziz, SSRN 4729284).
3. **Fixed 4%/8% brackets ignored per-name volatility.** A 4% stop is noise on a biotech gapper and enormous on a mega-cap.
4. **Claude judged headlines blind** — no technicals, no market context, no regime awareness. Same behavior in a bull tape and a crash.
5. **Zero intraday management.** Losers rode to the stop; winners could round-trip to red.
6. **Everything flattened at close**, surrendering the overnight/multi-day drift where most catalyst P&L accrues (PEAD is one of the oldest documented anomalies — Ball & Brown 1968 onward).
7. **No order verification, no retries** — one flaky Alpaca call could silently drop a trade or a candidate.
8. **Risk gate was shadow-only.** Its veto rules (pump profile, extended gap, low conviction) never protected anything.
9. **No weekly circuit breaker, no portfolio-level heat cap.**
10. **No backtesting layer at all.**
11. **Workflows alerted on success but not on failure.**
12. **Dashboard had no expectancy, profit factor, or drawdown** — the three numbers that actually say whether a system works.

## What changed

**Strategy engine**
- `regime.py` (new): SPY 200/50-SMA trend + 20d realized vol → BULL_QUIET / BULL_VOLATILE / CHOP / BEAR / CRISIS. Regime scales position risk (1.0x → 0x) and CRISIS blocks new entries. Trend filters of this type are well documented to remove most bear-market damage from long strategies.
- `analyst.py`: Claude now sees regime + per-name technicals and assigns each trade to a module:
  - **DAY_MOMENTUM** — v1 strategy, upgraded filters, flat by close.
  - **SWING_CATALYST** — 1–5 day PEAD-style holds, hard catalysts only (earnings/M&A/FDA/contract), GTC bracket, 0.75x risk, max 2 concurrent. This is the change that stops surrendering overnight gaps.
- Composite `setup_score` with a hard `MIN_SETUP_SCORE=6` gate; sub-threshold trades are discarded mechanically.
- Risk gate promoted from shadow to **blocking** (`GATE_BLOCKING=True`).

**Scanner**
- Enforces dollar-volume floor (bug fix), relative volume, ATR(14), extension-vs-20d-high filter (no chasing parabolic names), retry/backoff on all API calls, candidates sorted by rel-vol.

**Execution & risk**
- ATR-scaled brackets (1.5x stop / 3.0x target) clamped to 2–8% of entry.
- Sizing = risk% x regime multiplier x module scale, capped by 15% notional and a **3% portfolio heat cap** (sum of open entry→stop risk).
- **Weekly (rolling 5-session) −6% circuit breaker** in addition to the daily −3% halt.
- Order verification after submission; 5xx retries; `close_position` / `replace_stop` primitives.

**Midday manager (new session, 12:40pm ET)**
- Mechanical rules first: cut at ≤ −0.75R (dead thesis), raise stop to breakeven+ at ≥ +1.5R, swing time stop at 5 days. Claude then judges the remainder HOLD/CLOSE against fresh news — it can only close or hold, never add or widen.

**Backtesting (`backtest.py`, new)**
- Simulates the judgment-free mechanical floor (gap entry + ATR bracket, day and swing variants) over ~2 years of daily bars, pessimistic fills (stop assumed first when both touch), 10bps/side slippage, results grouped by regime and gap size. Live edge = live expectancy minus this floor.

**Automation**
- New `midday-manage.yml` workflow (holiday-aware, ntfy notification).
- Morning workflow now alerts on **failure**, high priority.
- `outcomes.py` is swing-aware: `OPEN_SWING` rows for positions still open, `SWING_CLOSED` rows when prior-day swings realize.

**Dashboard**
- New analytics KPI row: expectancy, profit factor, max drawdown, avg win, avg loss, current regime (+ risk multiplier and vol).
- Closed-trade stats include swing closures; experiment clock reset to v2 day 1.

## What is still hypothesis, not proof

- **Claude's catalyst judgment adds edge** — the core hypothesis. Measured as live expectancy vs. the backtest floor and vs. the rejected-candidate counterfactuals already logged.
- **Swing module survives gap risk** — PEAD is well documented but has attenuated in recent decades; 0.75x sizing is a guess pending data.
- **Midday cut threshold (−0.75R)** — plausible, untested; watch whether cut trades would have recovered.
- **Regime multipliers** are literature-informed round numbers, not fitted parameters. Overfitting risk is low precisely because nothing was fitted — but they may be miscalibrated.

## v2 evaluation framework (next 90 days)

Track daily: equity, heat, regime, per-module P&L. Weekly (self-review already automated): expectancy, profit factor, win rate, avg hold, gate-veto counterfactuals, midday-cut counterfactuals.

Decision thresholds at day 45 / day 90:
- **Working:** expectancy > +0.3%/trade after the backtest floor, profit factor > 1.3, max DD < 10%, and beats SPY buy-and-hold.
- **Judgment layer works, mechanics don't:** taken trades beat rejected counterfactuals but total P&L flat → tune brackets, keep the brain.
- **Needs revision:** expectancy ≤ floor for 30+ closed trades → the judgment layer isn't paying for itself; rethink before any thought of real money.

Run `python backtest.py` once with your Alpaca keys to print the floor before day 1.
