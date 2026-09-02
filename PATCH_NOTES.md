# Claude Trader — verdict tape staleness, 2026-09-02

Two files:

```
index.html    selftest.py
```

Applies on top of everything already pushed. Both are newer versions of files
you have; overwrite them. Nothing else changes.

---

## Why FRMI is still at the top

It isn't a bug in the tape. `logs/decisions.jsonl` has 21 entries and the newest
is **2026-08-11**. The tape shows the newest verdict on file, and that verdict is
FRMI. There has been no verdict since, because the scanner was locked out by the
duplicate-run guard from Aug 12 until you deployed the fix today.

Confirmed live on `main` — every patch is deployed:

```
run_morning.py   benchmark-first: YES
benchmark.py     backfill: YES
analyst.py       module-block: YES
outcomes.py      logged-module: YES
index.html       module-driven ledger: YES
```

The current `logs/health.json` still shows `scan SKIPPED — "duplicate run guard —
orders already placed today"`. Note the wording: that's the **old** string. The
patched code writes `"entry orders already placed today"`. So that entry is from
this morning's 16:41 UTC run, which used the pre-deploy code. No morning run has
executed since you pushed.

Next scheduled run is **12:10 UTC tomorrow (8:10am ET)**. That's when the tape
gets a new verdict. Nothing to fix to make that happen.

---

## What actually needed fixing

`LATEST VERDICT — TUE, AUG 11` rendered identically whether that was today or
three weeks ago. Literally true, completely misleading. FRMI read as the current
pick for three weeks while the scanner produced nothing, and the only honest
signal was the small `asOf` line in the header, far from the tape.

Same failure as the health banner and the trade ledger: a panel presenting old
data as current.

The tape now marks itself when the newest verdict is more than one trading day
old:

- Amber badge on the eyebrow: `stale · 16 trading days ago · no verdict since`
- Verdict text dimmed to 62%, candidate chips to 55%
- A dashed amber chip appended: *not today's screen — the scanner has not produced a verdict since Tue, Aug 11*

Rendered against your real `decisions.jsonl`:

```
date : LATEST VERDICT — TUE, AUG 11 stale · 16 trading days ago · no verdict since
chips: ✓ FRMI gap +18.1%  ·  not today's screen — the scanner has not
       produced a verdict since Tue, Aug 11
tape class: wrap tape-inner is-stale
```

Once tomorrow's run writes a fresh verdict the badge disappears on its own.

`selftest.py` gains section [10] — four checks mirroring the staleness threshold
so the dashboard and the suite can't drift apart. **Full suite: 46 PASS.**

---

## Verified

- `index.html` script block — `node --check` clean
- Rendered in jsdom against real `decisions.jsonl`; badge, dimming and `is-stale` class all applied
- `selftest.py` — 46 PASS
- Live `main` inspected file by file to confirm every prior patch is deployed

---

## Tomorrow morning, what to look for

1. Tape shows a new date with no stale badge.
2. `health.json` → `scan` reads `OK` with a candidate count, not `SKIPPED`.
   If it does say SKIPPED, check the wording — `"entry orders already placed
   today"` means the new guard fired for a real reason; the old string would
   mean the deploy didn't take.
3. `benchmark.csv` gains rows and backfills the 26 missing weekdays.
4. NTRA flips from `open · OVERSTAY` to a realized figure once the evening
   review runs tonight.

If you want to test the guard before then, `workflow_dispatch` on Morning
Trading Run from the Actions tab will do it — but be aware that fires a real
scan mid-session with a scanner built for the open, and it can place paper
trades. Waiting for 8:10am is the cleaner test.
