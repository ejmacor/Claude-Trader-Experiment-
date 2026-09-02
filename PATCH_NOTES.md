# Claude Trader — final package, 2026-09-02

Six files. Drop into `dev/`, overwrite, commit, push.

```
analyst.py     outcomes.py    executor.py
health.py      selftest.py    index.html
```

**Supersedes fix package #2.** If you already unzipped that one, these files
replace those — `health.py` and `selftest.py` are new additions found during this
audit, and `index.html` changed again (the ledger label is now module-driven
rather than reading a historical outcomes label). Nothing else in `dev/` is
touched: `config.py`, `run_morning.py`, `preflight.py`, `protection_check.py`,
`run_eod.py`, `scanner.py`, `regime.py` and the rest are all unchanged.

---

## What the audit actually checked

- **`dev/` vs live `main`** — every `.py`, `.html`, `.yml`, `.txt` and `.md`
  compared against `raw.githubusercontent.com`. All code identical. The five
  journal files differ by CRLF line endings only (Windows checkout), which is
  cosmetic. **Package #1 is already deployed to `main`** — `run_morning.py` and
  `health.py` on the live repo carry `DEAD_ORDER_STATUSES` and
  `_ledger_age_trading_days`.
- **All 20 modules compile and import cleanly** with the patch applied.
- **All 7 workflow YAML files parse.**
- **`selftest.py` — 33 checks, all pass** on the patched tree.
- **Dashboard rendered headlessly (jsdom) against your real log files.** Banner,
  verdict tape, KPI strip, 9-row trade ledger, and 18 decision-wire entries all
  render with no JS errors. The only console error is jsdom's missing canvas
  package, which is a test-harness limitation, not a dashboard bug.

---

## Two things this audit found that the earlier packages missed

### 1. `selftest.py` was failing, and it wasn't your code

```
FAIL  trading_days_since counts weekdays only
      expected 2, got 3
```

The assertion pinned `trading_days_since("2026-08-28")` to `2` against the real
wall clock. Fri Aug 28 to Tue Sep 1 is two weekdays, so it passed the day it was
written and has failed every day since. Nothing was broken.

This is the more serious version of the problem you already had. A suite that
cries wolf is a suite you stop reading, and "I ran selftest, it had a failure,
that one's always been there" is exactly how seven weeks of naked positions go
unnoticed.

`trading_days_since(date_str, today=None)` now takes an injectable date —
default unchanged, no production caller passes it. The assertion is pinned to a
fixed calendar and there are now three of them (Fri to Tue, Fri to Mon, same-day).

### 2. The ledger label was reading the wrong source

Fix #2 had the dashboard decide swing-vs-overstay from the `OPEN_SWING` /
`OPEN_OVERSTAY` action in `outcomes.csv`. That inherits every historical
mislabel — NTRA is logged `OPEN_SWING` from 2026-08-07 but was executed
`DAY_MOMENTUM` / `day`. Rendered against your real data it showed
`open · swing`, which is the wrong answer.

It now reads `module` straight off the `trade_log.csv` row that's already in
hand, falling back to the outcomes label only when module is absent. Verified
against real data: **NTRA now renders `open · OVERSTAY`.**

---

## New regression tests — section [8]

Seven checks that would have caught the original bug:

```
[8] Engine coherence — the 2026-07-10 to 2026-09-02 blind spot
  PASS  day-only prompt never offers SWING_CATALYST
  PASS  day-only prompt states the position is flat by the close
  PASS  swing-enabled prompt does offer SWING_CATALYST
  PASS  prompt horizon matches the bracket time_in_force
  PASS  a real swing is labelled OPEN_SWING
  PASS  a day trade still open is NOT labelled a swing
  PASS  an unknown symbol defaults to not-a-swing
```

I ran these against the **unpatched** tree to confirm they aren't tautologies —
three fail, and they fail gracefully with a clear message rather than a
traceback, so a partially-updated tree reports FAIL instead of crashing.

The fourth check is the one that matters long-term: it asserts the prompt's
stated horizon agrees with the TIF `place_bracket` will actually choose. If those
two ever drift apart again, the suite says so.

---

## Carried over from fix #2 (unchanged in substance)

- **`analyst.py`** — module block built from `config.SWING_ENABLED` via
  `_module_block()`. With swing off the analyst is told the trade is flat by the
  close and that this governs *selection*, not just execution. JSON schema enum
  and `intended_hold_days` hint follow the same flag. Demotions recorded on
  `decision["demotions"]` with reason strings, flagged per-trade, and appended to
  `market_note` as `[ENGINE: ...]`.
- **`outcomes.py`** — `_logged_module()` reads the real module from
  `trade_log.csv`. `OPEN_SWING` only for genuine swings; everything else still
  open is `OPEN_OVERSTAY`. Unknown module defaults to overstay. De-dupe checks
  both labels.
- **`executor.py`** — the silent demotion in `place_bracket` now prints, and the
  return carries `demoted_in_executor` and `protection_expires`.
- **`index.html`** — gap chip labelled (`gap +18.1%`, negative signs handled),
  banner splits halted from degraded with the cold-start clamp, ledger shows
  `open · OVERSTAY` in amber, expanded rows carry an **Executed as** line with
  module and TIF.

`config.py` is still untouched. `SWING_ENABLED` stays `False`. Flipping it is a
live strategy decision and everything here follows the flag either way.

---

## Deploy

1. Unzip over `dev/`, overwrite all six.
2. `python selftest.py` — expect **33 PASS, All checks passed.**
3. Commit and push in GitHub Desktop.
4. Hard-refresh the dashboard. Expect: no red banner, `gap +18.1%` on the FRMI
   chip, NTRA reading `open · OVERSTAY`, and an **Executed as** line when you
   expand any row.

Step 2 is the one worth not skipping. If it reports anything other than 33 PASS,
stop and send me the output before pushing.

---

## Still open — unchanged, still not a code problem

`logs/eod.jsonl` timestamps are 23:11 and 23:35 UTC (7/15 to 7/16) and 22:06 UTC
(9/1) — 7:11pm, 7:35pm, 6:06pm ET. The cron in `eod-flatten.yml` is 19:50/19:58
UTC, correctly ten minutes before the close. EOD flatten lands two to three hours
late whenever it runs.

That's the operational half of the same failure: the day-TIF bracket dies at 4pm
and the job meant to beat it arrives at 6. `preflight` and `position-sweep` cover
the morning side and are working — the NTRA re-arm on 9/1 and the sweep on 9/2
are both in the logs. Nothing in this zip makes GitHub Actions punctual.

Two options when you want it closed: an external scheduler hitting
`workflow_dispatch`, or make `run_eod.py` refuse to report OK when it runs after
4pm ET so the watchdog catches the drift. Say which and I'll build it.

One smaller thing I left alone: `DAY 59 / 90` is computed live from the start
date while the data underneath it is whatever last got committed. On Aug 12 the
counter kept climbing past a frozen wire. `asOf` already tells the truth
("as of Aug 11, 9:39 AM ET"), so it's not wrong, just easy to miss — worth
considering whether the day counter should freeze with the data.
