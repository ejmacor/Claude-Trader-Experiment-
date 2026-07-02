"""
STRATEGY CONFIG — FROZEN RULES
==============================
These rules were defined BEFORE day one of the experiment.
DO NOT EDIT during the 90-day run, or the results are meaningless.

Strategy: Pre-market catalyst interpretation plays.
Claude reads the news on pre-market gappers and decides which moves
are justified by the substance of the catalyst. Entries at the open,
mechanical bracket exits. One decision per day, no intraday babysitting.
"""

# ---- Experiment ----
EXPERIMENT_START = "2026-07-03"   # set to your actual day 1
EXPERIMENT_DAYS = 90

# ---- Account / Risk (paper) ----
STARTING_EQUITY = 100_000          # Alpaca paper default
RISK_PER_TRADE_PCT = 1.0           # % of equity risked per trade (stop distance based)
MAX_POSITION_PCT = 15.0            # no position larger than 15% of equity
MAX_TRADES_PER_DAY = 3             # Claude picks at most 3 setups
MAX_OPEN_POSITIONS = 3
DAILY_LOSS_HALT_PCT = 3.0          # if account down 3% on the day, no new trades

# ---- Universe filters (what qualifies as a candidate) ----
MIN_GAP_PCT = 4.0                  # pre-market gap of at least 4%
MIN_PRICE = 5.00                   # no sub-$5 stocks
MAX_PRICE = 500.00
MIN_AVG_DOLLAR_VOLUME = 5_000_000  # 20-day avg dollar volume, liquidity floor
MAX_CANDIDATES_SENT_TO_CLAUDE = 12 # top gappers by % move, capped

# ---- Trade mechanics ----
STOP_LOSS_PCT = 4.0                # bracket stop: 4% below entry
TAKE_PROFIT_PCT = 8.0              # bracket target: 8% above entry (2:1 reward/risk)
TIME_IN_FORCE = "day"              # everything flattens by close — this is day trading
ENTRY_TYPE = "market_on_open"      # decide pre-market, enter at the open

# ---- Claude API ----
CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS = 2000

# ---- Schedule (all times US/Eastern) ----
SCAN_TIME = "08:30"                # run the morning scan at 8:30am ET
MARKET_OPEN = "09:30"

# ---- Files ----
TRADE_LOG_CSV = "logs/trade_log.csv"
DECISION_LOG_JSONL = "logs/decisions.jsonl"   # every Claude decision, incl. NO_TRADE
EQUITY_LOG_CSV = "logs/equity_curve.csv"
