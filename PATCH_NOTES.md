# Claude Trader — fix package, 2026-09-02

Three files, full replacements. Drop into repo root, overwrite, commit, push.

```
run_morning.py
health.py
index.html
```

Base versions were pulled from `main` today, so these are current-with-upstream
as of 2026-09-02. If you've pushed anything since, diff before overwriting.

---

## 1. `run_morning.py` — the actual outage

**`already_ran_today()` counted every order, not just entries.**

```python
# before
return any(_et_date(o.get("submitted_at")) == today or
           _et_date(o.get("created_at")) == today for o in resp.json())
```

`/v2/orders?status=all` returns sells, closes, stop re-arms, and canceled and
rejected orders. So the moment EOD flatten, preflight remediation, or a stop leg
touched the order endpoint on a given day, the guard tripped and `scan` was
marked `SKIPPED` at step 0a — before regime, before candidates, before the
analyst. No verdict row was ever written. That's why the decision wire has been
frozen on Aug 11 while `logs/health.json` shows the morning run firing daily.

Now an order only counts as an entry if all four hold:

- `side == "buy"` (exits and housekeeping are sells)
- status not in `canceled / cancelled / expired / rejected / suspended / pending_cancel / pending_replace / replaced / stopped`
- symbol is **not** already in `executor.get_open_positions()` (that's an add or a cover, not a fresh entry)
- submitted today ET (unchanged)

**Also changed:** the guard now **fails open**. If the Alpaca call throws, it
logs `WARN` to health and continues instead of exiting. A false halt is
invisible and costs a whole trading day; a genuine duplicate is caught
downstream by the position check.

Caveat: the buy-side filter assumes entries are always long. If you ever add a
short module, this guard needs revisiting.

---

## 2. `health.py` — cold-start false alarm

`trading_days_since(None)` returns `999`. Both `scan` and `eod` have
`"last_ok": null`, and `first_seen` is `2026-09-01T22:06` — the ledger is one
day old. So `stale()` reported two stages as 999 days stale on a ledger that
had existed for a few hours.

Added `_ledger_age_trading_days()`. When `last_ok` is null, staleness is now
clamped to the ledger's own age. A stage cannot be staler than the file
recording it.

---

## 3. `index.html` — two display bugs

**Banner.** Same 999 clamp applied client-side, plus a real distinction:

- **Pipeline halted** (red) — a problem stage has stopped *executing*
- **Pipeline degraded** (amber) — stages are still running on schedule and merely reporting errors

Today's state is the second one. `morning`, `scan` and `preflight` all ran, and
`eod` ran last night; calling that HALTED was wrong. Rows now read
"ran today but has not reported a success yet" instead of "no successful run in
any recorded trading day(s)".

Dry-run against the live `logs/health.json`: **the banner hides entirely** (ledger
age 1 trading day, limit 2). Once the ledger is 3+ days old and `scan` still has
no `last_ok`, it escalates correctly to amber with the duplicate-guard detail.

**Verdict tape chip.** `gap_pct` was rendered unlabelled with a hardcoded `+`:

```js
<span class="g">+${Number(c.gap_pct).toFixed(1)}%</span>
```

That's the premarket gap at scan time, not P&L — which is how you got a green
`✓ FRMI +18.1%` sitting above a trade that closed −31.01%. Now renders
`gap +18.1%` with a tooltip, and negative gaps print their own sign instead of
`+-30.0%`.

---

## Verified before packaging

- `run_morning.py`, `health.py` — `ast.parse` clean
- `index.html` — script block extracted, `node --check` clean
- `executor.get_open_positions()` confirmed to exist (`executor.py:61`)
- new banner logic dry-run against live `logs/health.json`

---

## NOT fixed — the thing I'd look at next

FRMI: entry $6.95, stop $6.39 (−8.06%), closed −31.01%. It went through the
stop by 23 points and sat open Aug 11 → Sep 1. TWLO the same, Aug 7 → Sep 1.
Preflight reported `0 stop(s) re-armed` today, which reads like it isn't finding
brackets to re-arm at all rather than finding them all healthy.

Nothing in this package touches that. The stop path lives in
`executor.place_bracket` / `replace_stop` / `preflight`, and I'd be guessing
without seeing whether the bracket legs were ever accepted by Alpaca. Worth
pulling the order history for FRMI and checking whether a stop leg existed.

Also unaddressed: the morning run fired at 12:41pm ET today. A scanner designed
for 8:30am is screening a different tape four hours late.
