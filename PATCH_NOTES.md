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
