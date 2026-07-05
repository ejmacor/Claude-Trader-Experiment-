"""
STRATEGY CONFIG — v2 "COMPOSITE ENGINE"
=======================================
v2 rebuild (2026-07-05). Supersedes the v1 frozen config (kept at
config_v1_frozen.py.bak for the record). The v1 90-day test is
officially invalidated; the clean 90-day clock restarts below.

v2 strategy: multi-module catalyst engine.
  MODULE A — DAY_MOMENTUM: catalyst gap continuation, ATR bracket,
             flat by close. (v1 strategy, upgraded filters.)
  MODULE B — SWING_CATALYST: PEAD-style 1–5 day holds on hard
             catalysts (earnings beat + raised guidance, M&A, FDA),
             GTC ATR bracket + time stop. Captures overnight drift.
Both are regime-gated and sized by volatility.
"""

# ---- Experiment ----
EXPERIMENT_START = "2026-07-06"    # v2 day 1 — fresh 90-day clock
EXPERIMENT_DAYS = 90
CONFIG_VERSION = "2.0"

# ---- Account / Risk (paper) ----
STARTING_EQUITY = 100_000
RISK_PER_TRADE_PCT = 1.0           # % equity risked to the stop, before regime scaling
MAX_POSITION_PCT = 15.0            # hard per-name notional cap
MAX_TRADES_PER_DAY = 3
MAX_OPEN_POSITIONS = 4             # room for day + swing overlap
MAX_PORTFOLIO_HEAT_PCT = 3.0       # sum of open risk (entry->stop) across positions
DAILY_LOSS_HALT_PCT = 3.0
WEEKLY_LOSS_HALT_PCT = 6.0         # rolling 5-session halt (circuit breaker)

# ---- Regime gating (computed in regime.py from SPY) ----
# trend: SPY close vs 200-SMA and 50-SMA;  vol tier: 20d realized vol annualized
REGIME_RISK_MULT = {               # multiplies RISK_PER_TRADE_PCT
    "BULL_QUIET":   1.00,          # above 200 & 50 SMA, vol < 18%
    "BULL_VOLATILE":0.60,          # above 200 SMA, vol 18-30%
    "CHOP":         0.50,          # mixed trend signals
    "BEAR":         0.35,          # below 200 SMA — long catalyst plays fade fast
    "CRISIS":       0.00,          # vol > 40%: no new longs, manage exits only
}
VOL_QUIET_MAX = 18.0               # annualized 20d realized vol thresholds (%)
VOL_ELEVATED_MAX = 30.0
VOL_CRISIS_MIN = 40.0

# ---- Universe filters ----
MIN_GAP_PCT = 4.0
MIN_PRICE = 5.00
MAX_PRICE = 500.00
MIN_AVG_DOLLAR_VOLUME = 5_000_000  # 20-day avg $ volume — NOW ENFORCED (v1 bug: defined, never checked)
MIN_RELATIVE_VOLUME = 1.5          # today's volume pace vs 20d avg ("stocks in play" filter,
                                   # the strongest documented ORB/momentum edge enhancer)
MAX_EXTENSION_FROM_20D_HIGH = 25.0 # skip names already >25% above their 20d high pre-gap
MAX_CANDIDATES_SENT_TO_CLAUDE = 12

# ---- Trade mechanics: volatility-based brackets (replaces fixed 4%/8%) ----
ATR_LOOKBACK_DAYS = 14
STOP_ATR_MULT = 1.5                # stop = entry - 1.5x ATR
TARGET_ATR_MULT = 3.0              # target = entry + 3.0x ATR (keeps 2:1 R/R, scaled to the name)
STOP_PCT_FLOOR = 2.0               # bracket never tighter than 2% / wider than 8% of entry
STOP_PCT_CEIL = 8.0
ENTRY_TYPE = "market_on_open"

# ---- Module A: DAY_MOMENTUM ----
DAY_TIME_IN_FORCE = "day"          # flat by close

# ---- Module B: SWING_CATALYST ----
SWING_ENABLED = True
SWING_TIME_IN_FORCE = "gtc"        # bracket persists overnight
SWING_MAX_HOLD_DAYS = 5            # time stop: evening job closes anything older
SWING_RISK_SCALE = 0.75            # swing carries gap risk -> 75% of normal risk unit
SWING_MAX_POSITIONS = 2
SWING_ALLOWED_CATALYSTS = {"earnings", "ma", "fda", "contract"}  # hard catalysts only

# ---- Midday management session (12:30pm ET) ----
MIDDAY_ENABLED = True
MIDDAY_CUT_THRESHOLD_R = -0.75     # position at/below -0.75R by midday -> cut (dead thesis)
MIDDAY_TIGHTEN_TRIGGER_R = 1.5     # position at/above +1.5R -> raise stop to breakeven+

# ---- Quality gate (was shadow-only in v1; now blocking) ----
GATE_BLOCKING = True               # shadow_gate vetos now actually veto
MIN_SETUP_SCORE = 6                # Claude's composite score must clear this

# ---- Claude API ----
CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS = 2500

# ---- Files ----
TRADE_LOG_CSV = "logs/trade_log.csv"
DECISION_LOG_JSONL = "logs/decisions.jsonl"
EQUITY_LOG_CSV = "logs/equity_curve.csv"
REGIME_LOG_JSONL = "logs/regime.jsonl"
