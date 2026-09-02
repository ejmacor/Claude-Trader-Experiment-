# Claude Trader — benchmark fix, 2026-09-02

Three files. Drop into `dev/`, overwrite, commit, push.

```
benchmark.py    run_morning.py    selftest.py
```

Applies on top of the FINAL package. `analyst.py`, `outcomes.py`, `executor.py`,
`health.py` and `index.html` from that zip are unchanged — keep them.
`selftest.py` is in both; this version is newer (adds section [9]).

---

## What was wrong

`logs/benchmark.csv` stops at 2026-08-11. Two independent causes, both fixed.

**1. No code in the repo could fill a gap.** `log_spy()` called
`/v2/stocks/SPY/bars/latest` — one bar, today. There was no range fetch
anywhere. Once a row was missed it was missed permanently, and the only way to
repair it was by hand.

**2. The write sat behind an early exit.** It was at step 0d, deliberately ahead
of the guardrail exit (the comment at line 92 says so). But the duplicate-run
guard exits at 0a, above it. Every morning that tripped the guard from Aug 12
onward also skipped the benchmark write.

## What changed

**`benchmark.py`** — rewritten.

- `fetch_range(start, end)` — SPY daily bars over a window, paginated. Non-trading days simply don't come back, so no synthetic rows.
- `backfill(start, end, dry_run)` — merges a window in. Never overwrites an existing row; a backfill repairs history, it doesn't rewrite it. With no window it runs from the last logged row to today.
- `write_rows()` — rewrites the file sorted by date. Required once backfilling exists: the dashboard plots in file order, so an appended 08-12 landing after 08-11 would make the SPY line zigzag backwards.
- `gaps(through=None)` — scans from the first row **through today**, not to the last row. The hole here is a trailing one and a first-to-last scan is blind to exactly that shape.
- CLI: `--backfill [START END]`, `--dry-run`, `--check`.

**`run_morning.py`** — benchmark moved to step 0a, ahead of every `sys.exit()` in
the function (verified: benchmark at line 117, first exit at line 140). It now
also self-heals — if `gaps()` finds missing rows it backfills before logging
today, so a future outage closes itself instead of leaving a permanent hole.

**`selftest.py`** — new section [9], eight checks. Full suite is now **42 PASS**.

---

## What your file actually looks like

```
rows: 17   first: 2026-07-06   last: 2026-08-11
weekday gaps: 26
  interior (10): 2026-07-14, 2026-07-27..31, 2026-08-03..06
  trailing (16): 2026-08-12 .. 2026-09-02
```

The trailing 16 is the outage we already knew about. **The interior 10 is new
information** — the morning run was already missing days in late July and early
August, before the duplicate-guard lockout started. Whatever was wrong began
earlier and more intermittently than the Aug 12 story suggests. Worth a look
before the day-91 writeup.

---

## Deploy

1. Unzip over `dev/`, overwrite the three files.
2. `python selftest.py` — expect **42 PASS**.
3. Dry run first, to see what it would pull without writing:
   ```
   python benchmark.py --backfill --dry-run
   ```
4. If the dates look right:
   ```
   python benchmark.py --backfill
   python benchmark.py --check
   ```
   `--check` should report 0 gaps, or only market holidays.
5. Commit and push. The SPY line will run to today.

Step 3 needs live Alpaca credentials in your shell, same as any other script
here. If you'd rather not run it locally, pushing alone is enough — the next
morning run detects the gaps and backfills on its own.

---

## Verified before packaging

- `benchmark.py`, `run_morning.py`, `selftest.py` — `ast.parse` clean; all 20 modules compile and import
- Backfill exercised with a stubbed feed: merge, sort, no-overwrite, no duplicates, single header, dry-run writes nothing, re-run is a no-op
- `gaps()` run against your real `benchmark.csv` — found the trailing hole the old first-to-last logic missed
- Benchmark call confirmed ahead of every `sys.exit()` in `run_morning.main()`
- Dashboard re-rendered in jsdom against real logs — no regression

---

## Still open

EOD flatten still lands 6–7pm ET against a 3:50pm cron. Unchanged.

One caution if you want the `run_eod.py` version of that fix: if EOD runs late
*every* day, marking it ERROR every day gives you a permanently red banner —
which is the same crying-wolf failure as the stale selftest assertion. The right
shape is a distinct "ran late" detail that's visible without reading as a halt.
Tell me which and I'll build it that way.
