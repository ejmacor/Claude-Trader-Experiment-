# Claude Trader — a blocked day still reports the market, 2026-09-02

```
run_morning.py    selftest.py
```

Overwrite both in `dev/`, commit, push, **then** run the workflow.

Supersedes the `run_morning.py` in the get-it-trading zip. `config.py` and
`scheduler/` from that package are unchanged — deploy those as-is.

---

## What changed

**`regime_note()`** builds the day's market note from live regime data. SPY's
price, its 50/200 SMAs and 20-day realized vol are all current — none of them
depend on the stale pre-market gap screen — so a day the system cannot trade
can still say something true about the tape, then say why it stood down.

Before:

```
[ENGINE: no entries — run started 16:05 ET, past the 10:30 entry cutoff]
```

After:

```
BULL_QUIET regime with SPY above both SMAs (765.94 vs 754.29 / 710.23),
realized vol 9.3%. No trades today — 2 candidate(s) screened but not
traded: run started 16:05 ET, past the 10:30 entry cutoff.
```

## The second bug I found while wiring it

The **guardrail-halt path wrote nothing to `decisions.jsonl` at all.**

It marked `health.json` correctly — the comment there even says a halt is a
real outcome, not silence — but `health.json` is what the watchdog reads. The
*dashboard* reads `decisions.jsonl`. So any day the risk guardrails halted the
run, the verdict tape stayed frozen on the last good day and looked exactly
like a dead pipeline.

That is the same failure that hid August, sitting in the code path specifically
written to prevent it. It now logs a real row too, led by the same market read:

```
BULL_QUIET regime with SPY above both SMAs (...). No trades today — no
entries: risk guardrail: <reason>.
```

## Degradation

`regime_note()` never raises. If `regime.classify()` fails or returns nothing,
it writes `UNKNOWN regime. No trades today — ...` rather than killing the run.
A `BEAR` tape is described as "below its 200d SMA" rather than borrowing the
constructive phrasing — asserted in the suite so it can't drift.

## selftest

Section [14], ten checks covering both paths, the degraded case, and the bear
wording. **Full suite: 81 PASS.**

## Verified

- Tree compiles, all modules import clean
- `regime_note()` exercised against your real `regime.jsonl` in on-time, late-with-candidates, late-with-none, and both degraded shapes
- 81 PASS

---

## Then run it

Actions -> Morning Trading Run -> Run workflow.

The workflow-level dedupe checks `decisions.jsonl` for a row dated today and
there isn't one, so it will proceed. It commits its own logs back, so the
dashboard updates on its own a minute or two later.

What you should see:

- stale badge gone, tape dated today
- headline: a real `BULL_QUIET ... realized vol 9.3%` read, then the no-trade reason
- SPY line on the equity chart reaching today (26 weekdays backfilled)
- `health.json` -> `scan: LATE` — which means **the duplicate guard is fixed**

If `scan` comes back `SKIPPED`, read the detail string. `"entry orders already
placed today"` is the new guard firing for a real reason; the old wording means
the deploy didn't take.
