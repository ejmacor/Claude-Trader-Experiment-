# Claude Trader — verdict tape redesign, 2026-09-02

```
index.html    selftest.py
```

Overwrite both in `dev/`. Nothing else changes.

---

## What I got wrong

My staleness fix marked the same fact three times — an amber badge, a dimmed
headline, and an extra chip explaining the chip next to it — while still giving
the three-week-old ticker top billing. The loudest element on the page was
still `✓ FRMI gap +18.1%`.

Labelling something misleading three times does not make it not misleading. I
added warnings to a wrong layout instead of fixing the layout.

## What it does now

When there is no current verdict, the tape leads with **that**, not with an old
pick wearing a warning label:

```
NO VERDICT SINCE TUE, AUG 11

The scanner has not produced a verdict in 16 trading days.
Nothing on this page is a current read on the market.

› Show the last verdict (Tue, Aug 11)
```

One statement instead of three. FRMI, its gap, and the Aug 11 market note move
inside the collapsed `<details>` — still there, one click away, clearly framed
as history rather than a recommendation.

The badge, the dimming and the duplicate chip are all gone, along with their
CSS.

**The fresh path is untouched.** Verified by appending a verdict dated today
and re-rendering:

```
LATEST VERDICT — WED, SEP 2
BULL_QUIET regime with SPY well above both SMAs...
✓ FRMI gap +18.1%
```

No badge, no collapsed block, chips inline as before. Once tomorrow's run
writes a verdict this reverts to normal on its own.

## selftest

Section [10] gains five checks that assert the markup contract, so a future
edit cannot quietly promote a stale verdict back to the headline:

```
PASS  stale tape leads with the absence of a verdict
PASS  the old verdict is collapsed, not headlined
PASS  the three-signal styling is gone
PASS  the duplicate not-today chip is gone
```

They read `index.html` relative to the script rather than the working
directory, since the suite runs in a tempdir.

**Full suite: 65 PASS.**

## Verified

- `node --check` clean on the script block
- Rendered in jsdom against real `decisions.jsonl` — collapsed by default, correct summary text
- Fresh-verdict path rendered separately and unchanged
- 65 PASS
