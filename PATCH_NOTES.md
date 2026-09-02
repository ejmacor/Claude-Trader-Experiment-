# Claude Trader — data integrity, 2026-09-02

Three files:

```
trade_logger.py    outcomes.py    selftest.py
```

Applies on top of everything in `dev/`. All prior patches verified present
before I started. `selftest.py` is newer than the copy you have — overwrite it.

Two bugs, both visible in the screenshot you sent, both in the data rather
than the display.

---

## 1. MAX DRAWDOWN is wrong — -16.3% shown, -6.9% actual

Every row in `equity_curve.csv` is an **intraday snapshot**. `log_equity()`
writes `account["equity"]` at whatever moment the run fires, and replaces the
day's row on each run, so the last run of the day wins. Usually harmless —
typically within ~0.5% of the close.

On 2026-07-28 it was not:

```
2026-07-27   96,729.60
2026-07-28   84,615.95   day_pnl_pct -12.805
2026-07-29   95,284.43   day_pnl_pct  -1.826
```

Those last two rows are mutually exclusive. If 07-29 really fell 1.826% to
95,284, then 07-28 closed at **97,056.69** — not 84,616. Cash sat unchanged at
53,481.97 all week, so nothing was sold. The account never closed near 84,616.

That single row is the cliff in your equity chart, and it sets the drawdown:

```
as logged      max DD -16.26%   (trough 2026-07-28)
without it     max DD  -6.94%   (trough 2026-09-01)
```

A bad intraday mark has been driving the headline risk number for five weeks.

**A note on how I got here.** My first attempt used a drift threshold to spot
bad rows. At 0.25% it flagged 13 rows — 11 of them normal pre-open snapshots
sitting a half-percent from the close. That's the same crying-wolf failure as
the stale selftest assertion, so I threw it out. Thresholds on this data are
guesswork.

The fix uses authority instead. `last_equity` **is** the settled prior close,
so it wins unconditionally, no threshold. Today's row stays a live snapshot
(the dashboard needs it); every prior row becomes a true close the next
morning. `settle_previous_close()` only fires when the newest stored row is the
immediately preceding weekday, so a gap in the log can never overwrite an old
row with a much later close.

For history, `repair_equity()` pulls Alpaca's own
`/v2/account/portfolio/history` daily series and rewrites stored closes from
it. That's the account's real record, not a guess.

## 2. The judgment readout has been showing nothing

Your screenshot:

```
BY CATALYST              other x8         13% · avg -6.42%
CONVICTION CALIBRATION   Low (1-6) x8          -6.42% avg
```

`SWING_CLOSED` rows were written with `conviction` and `catalyst_type` blank —
and those are the **only** rows carrying realized P&L. So the panel that
answers "does Claude's conviction predict outcomes", which is the point of the
whole experiment, had nothing to group by and dumped all eight trades into
`other` and `Low`.

Joined properly from `trade_log.csv`:

```
BY CATALYST
  earnings   x5    20%   avg  -1.64%
  contract   x2     0%   avg -19.09%
  fda        x1     0%   avg  -4.98%

CONVICTION CALIBRATION
  Low (1-6)  x1   avg -31.01%
  Med (7)    x5   avg  -1.84%
  High (8+)  x2   avg  -5.57%
```

Contract catalysts averaging -19% against earnings at -1.6% is a real signal
that was invisible. So is the fact that the single conviction-6 trade was by
far the worst.

`_logged_trade()` now carries conviction and catalyst onto the closing row, and
`--repair-metadata` backfills the eight rows already written blank.

---

## Deploy

1. Unzip over `dev/`, overwrite all three.
2. `python selftest.py` — expect **60 PASS**.
3. Repair history (needs live Alpaca creds, same as any script here):
   ```
   python trade_logger.py --dry-run
   python outcomes.py --repair-metadata --dry-run
   ```
   Then drop `--dry-run` on both.
4. `python trade_logger.py` on its own prints rows and max drawdown — expect it
   to land near **-6.9%**.
5. Commit and push.

Both repairs are idempotent and safe to re-run. `repair_equity` never touches
today's row.

## Verified

- All three files parse; whole tree compiles and imports clean
- Equity repair exercised with a stubbed history: 1 row rewritten, DD -16.26% -> -6.94%, today's row untouched
- Metadata repair run against your real `outcomes.csv`: all 8 rows backfilled correctly, dry-run writes nothing, re-run is a no-op
- Dashboard re-rendered in jsdom against the repaired data — `kDD` now reads **-6.9%**
- selftest sections [11] and [12] added, 60 PASS total

## Still open

Unchanged: EOD flatten still lands 6-7pm ET against a 3:50pm cron. And the
`TRADES TAKEN` subtitle still reads "ATR brackets · day + swing" — cosmetic,
but wrong now that the engine is day-only.
