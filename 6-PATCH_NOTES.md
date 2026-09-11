# Claude Trader - v2.2 tuning patch, 2026-09-11

Approved by the user after the trade-restriction review of the first 9 closed
trades (2W/7L, equity -6.1%) and 22 sessions of funnel data.

## What changed and why

1. **MIN_AVG_DOLLAR_VOLUME: $5M -> $2M** (config.py)
   The $5M floor caused 325 of ~380 filter rejections and left 13 of 22
   sessions with zero candidates - the main reason the system barely trades.
   Miscalibrated to a ~$100k paper account risking 1% with a 15% notional
   cap: $2M/day is still >130x the largest possible position.

2. **MIN_RELATIVE_VOLUME is now actually enforced** (scanner.py)
   Config defined 1.5 since v2.0 but scanner.py never checked it - the same
   class of bug as v1's unused liquidity floor. Entries were taken on dead
   tape (TWLO rel vol 0.06 at entry, a loss). Enforcement is pace-adjusted
   via expected_volume_fraction(): the floor is 1.5x the fraction of a
   normal day's volume that should be done by the time the run executes, so
   a pre-open run doesn't compare an almost-empty daily bar against a
   full-day average. Measured against true live volume summed from 1-minute
   bars (daily-bar premarket coverage is feed/condition dependent), with
   the daily-bar proxy as fallback.

3. **Contract catalysts disabled** (config.py, analyst.py, shadow_gate.py)
   FRMI -31.0% (conviction 6) and IREN -7.2%; contract trades averaged
   about -19% vs -1.6% for earnings. Contract PRs headline dollar figures
   with no verifiable economics - the catalyst type this engine judges
   worst. config.BLOCKED_CATALYST_TYPES = {"contract"}, enforced three
   ways: the analyst prompt excludes them, analyze() hard-filters them,
   and the gate vetoes them as a backstop. Revisit with a bigger sample.

4. **No-overnight guarantee: scheduler redundancy + flat check**
   (morning-run.yml, eod-flatten.yml, evening-review.yml)
   Investigation: run_eod.py, the position sweep, and preflight are sound -
   the overnight holds (IREN 17 days, FRMI's -31% gap) happened because the
   scheduled jobs themselves did not fire (GitHub cron landing 2-5h late or
   skipping slots; the Cloudflare dispatcher worker silent). Repo-side:
   - morning-run: third cron slot (12:40 UTC), all still before the 10:30
     ET entry cutoff (cutoff unchanged - stale-screen trades stay banned)
   - eod-flatten: post-close 20:10 UTC backstop so a skipped in-session
     slot still queues the closes for the next open
   - evening-review: verifies the account is actually flat after the close
     and pages high-priority if not (a SKIPPED slot alerts nobody)
   Not repo-side: the Cloudflare worker's GitHub token is in the user's
   Cloudflare dashboard - that fix restores the precise clock.

5. **Kept, deliberately**: the 10:30 ET entry cutoff, all risk limits
   (1% risk, 3% daily halt, 6% weekly breaker, 3% heat cap, MIN_SETUP_SCORE
   6). Losses came from signal quality and dead tape, not from risk sizing.

---

# Claude Trader — journal panel off the GitHub API, 2026-09-02

```
self_review.py    index.html    selftest.py    journal/index.json
```

`journal/index.json` goes in `journal/`. It is generated and verified against
the eight notes actually on `main`.

---

## Why it was empty, and why it came back

The panel listed the folder via `api.github.com/repos/.../contents/journal` —
the **unauthenticated** GitHub API, capped at **60 requests per hour per IP**.
Every page load spent one.

Past the cap it returns 403. The loader said:

```js
if(!r.ok) return;   // folder doesn't exist yet — empty state stands
```

So a rate-limit error rendered as **"No entries yet"**, identical to a genuinely
empty folder. Eight notes were sitting in the repo the whole time. It loaded for
you a few minutes later because the hourly window rolled over — nothing was
fixed, and it will do the same thing again.

Same failure class as the rest of today: a failure that renders as an
innocuous empty state.

**It also took the brain map down.** `renderBrain(notes)` is called inside
`loadJournal()` after the fetch, so when the listing failed the graph never
drew at all. Not frozen — absent.

## The fix

`self_review.py` now writes `journal/index.json` after every review, and takes
`--reindex` to build it on demand:

```json
{ "generated": "...", "count": 8, "files": ["2026-07-03-launch.md", ...] }
```

The dashboard reads that from `raw.githubusercontent.com`, which is CDN-backed
and uncapped. The API stays as a fallback — but its failure is now **reported**
instead of swallowed:

> **Journal couldn't load** — GitHub API rate limit reached (60/hour per IP)
> and journal/index.json is missing. The notes are in the repo under /journal —
> this panel could not list them.

Verified by rendering in jsdom with the API forced to 403:

```
=== ANALYST JOURNAL ===
entries rendered: 8
  2026-08-14-weekly-self-review
  2026-08-07-weekly-self-review
  2026-07-24-weekly-self-review
  2026-07-17-weekly-self-review
```

Eight entries, newest first, with the API returning nothing.

`write_index()` never raises — a disk error prints and returns `[]` rather than
taking the weekly review down with it. Asserted in the suite.

**Full suite: 98 PASS.**

---

## Separate issue: the Aug 14 review contains false premises

Worth knowing before day 91. That entry says:

> The 4% target was never touched and the 8% stop was never triggered.
> The position is still open as a swing.

Both are wrong, and the trade log says so:

```
2026-08-11,FRMI,...,module=DAY_MOMENTUM,time_in_force=day
```

- It was **not a swing.** `SWING_ENABLED` was False; the analyst's swing pick was silently demoted. The same entry even notes "full DAY_MOMENTUM sizing" two paragraphs later without reconciling the contradiction.
- The stop was **not untriggered — it was cancelled.** A day-TIF bracket leg expires at the close. From Aug 12 the position had no stop at all.
- The entry grades it "sound reasoning, weak execution context" on a +0.64% open-to-close. It closed at **-31.01%** on Sept 1.

The reviewer wasn't wrong to reason as it did — it read the logs it was given,
and those logs said `OPEN_SWING`. The mislabel propagated into the analysis.

Nothing to patch. The journal is deliberately write-only and rewriting it would
defeat that. But when you write the day-91 summary, the Aug 14 entry's read on
FRMI needs a correction alongside it.

## Verified

- `self_review.py` — `ast.parse` clean; whole tree compiles
- `index.html` — `node --check` clean
- Rendered in jsdom with the GitHub API stubbed to 403: 8 entries, newest first
- Error path rendered separately: reports the cause instead of the empty state
- Every filename in `journal/index.json` confirmed to resolve on `main`
- 98 PASS
