# Data repair — drop into `logs/`, 2026-09-02

```
logs/outcomes.csv
logs/equity_curve.csv
```

These are your **live files from `main`**, downloaded just now, with the two bad
rows repaired. Overwrite the two in `dev/logs/`, commit, push. Nothing else
changes — CRLF line endings preserved, row counts identical.

I ran the repair for you instead of you running the CLI, because
`trade_logger.py --repair` needs live Alpaca credentials and `outcomes.py`
imports `executor`, which reads the API key at import time even though the
metadata repair itself never touches the network.

---

## Why the dashboard didn't change

The code patch was pushed and is correct. But it only governs rows written
**from now on**. The rows already in the CSVs stay wrong until repaired, and
the dashboard reads those CSVs from `raw.githubusercontent.com`. Fixed code,
unfixed data.

---

## Exactly what changed — 9 lines, nothing else

**`logs/outcomes.csv`** — 8 rows, conviction and catalyst backfilled from
`trade_log.csv`:

```
- 2026-07-15,PENG,SWING_CLOSED,,,,,-0.92
+ 2026-07-15,PENG,SWING_CLOSED,7,earnings,,,-0.92
- 2026-07-17,AEHR,SWING_CLOSED,,,,,-9.88
+ 2026-07-17,AEHR,SWING_CLOSED,7,earnings,,,-9.88
- 2026-08-06,IREN,SWING_CLOSED,,,,,-7.18
+ 2026-08-06,IREN,SWING_CLOSED,7,contract,,,-7.18
- 2026-08-06,SMCI,SWING_CLOSED,,,,,-6.67
+ 2026-08-06,SMCI,SWING_CLOSED,8,earnings,,,-6.67
- 2026-08-06,ARWR,SWING_CLOSED,,,,,-4.98
+ 2026-08-06,ARWR,SWING_CLOSED,7,fda,,,-4.98
- 2026-08-06,THC,SWING_CLOSED,,,,,13.75
+ 2026-08-06,THC,SWING_CLOSED,7,earnings,,,13.75
- 2026-09-01,TWLO,SWING_CLOSED,,,,,-4.47
+ 2026-09-01,TWLO,SWING_CLOSED,8,earnings,,,-4.47
- 2026-09-01,FRMI,SWING_CLOSED,,,,,-31.01
+ 2026-09-01,FRMI,SWING_CLOSED,6,contract,,,-31.01
```

**`logs/equity_curve.csv`** — 1 row:

```
- 2026-07-28,84615.95,53481.97,-12.805
+ 2026-07-28,97056.69,53481.97,
```

That value is **not a guess**. The 2026-07-29 row carries Alpaca's own day
change of -1.826%, and 95,284.43 / (1 - 0.01826) = 97,056.69. It's derived from
Alpaca's record of what the account actually did. `day_pnl_pct` is blanked
because the old number was computed off the bad snapshot.

---

## What the dashboard will show after you push

Rendered in jsdom against these exact files:

```
MAX DRAWDOWN   -16.3%  ->  -7.0%

BY CATALYST
  earnings   x5    20% · avg  -1.64%
  contract   x2     0% · avg -19.09%
  fda        x1     0% · avg  -4.98%

CONVICTION CALIBRATION
  Low (1-6)  x1   avg -31.01%
  Med (7)    x5   avg  -1.84%
  High (8+)  x2   avg  -5.57%
```

The equity cliff disappears from the chart.

Two things worth reading in that breakdown once it's up: contract catalysts at
-19% against earnings at -1.6%, and the single conviction-6 trade being by far
the worst outcome. Neither was visible before.

---

## Verified

- Diffed against live `main` — 9 changed lines, no others
- Row counts unchanged (outcomes 23, equity 49); CRLF preserved, zero bare LF
- Dashboard rendered headlessly against these files: `kDD` reads -7.0%, ledger intact, 9 rows

Going forward the code handles this on its own — `settle_previous_close()`
overwrites each prior day with its settled close, and closing rows now carry
their own metadata. This is a one-time history repair.
