# Getting it trading again — 2026-09-02

Two problems. The guard fix is already deployed and correct; this closes the
other one.

```
run_morning.py    config.py    selftest.py    scheduler/
```

---

## The actual blocker

Every workflow is running 2-5 hours late, every day:

```
stage      scheduled          executed           delay
morning    12:10 UTC          16:41 UTC          +4h16m
preflight  12:10 UTC          17:17 UTC          +4h52m
eod        19:50 UTC          22:07 UTC          +2h09m
evening    23:10 UTC          00:59 UTC          +1h49m
```

GitHub Actions cron is best-effort, and on a public repo it queues behind
everything else. It is not a clock.

This matters more than it looks. The scanner screens **pre-market gaps** — gap
%, relative volume, extension vs the 20d high, all computed before the open —
and the analyst's thesis is same-session continuation. Running that at 12:41pm
ET is not the strategy. It is a different, untested one wearing the same name.

It is also half of the stop problem: EOD flatten arriving at 6:06pm is two
hours after the day-TIF brackets already expired at the close.

---

## Part 1 — `config.py` + `run_morning.py`: refuse to trade on stale data

```python
MAX_ENTRY_TIME_ET = "10:30"
```

Past that, the run **still does everything else** — benchmark, regime,
preflight remediation, candidate screening, decision logging. It places no new
entries, and it is loud about it:

- `health.json` gets `scan: LATE` and `execute: LATE` with the clock time and reason
- a real row goes to `decisions.jsonl` with `[ENGINE: no entries — run started 12:41 ET, past the 10:30 entry cutoff…]`

That last part is deliberate. A silent no-trade day is indistinguishable from
discipline, and that exact confusion is what hid the August outage for three
weeks. The wire has to show that the scanner ran, found N names, and was
blocked.

Verified across the clock:

```
08:12 ET -> entries allowed      10:31 ET -> BLOCKED
09:45 ET -> entries allowed      12:41 ET -> BLOCKED
10:29 ET -> entries allowed      16:41 ET -> BLOCKED
```

**Be aware of what this means on its own:** if you deploy only Part 1 and the
scheduling stays broken, the system will screen every day and trade never. That
is the correct failure direction, but it is not "working." Part 2 is the fix.

## Part 2 — `scheduler/`: a Cloudflare Worker that is the clock

You already run a Worker (`claude-trader-live.ejmacor.workers.dev`). Cloudflare
cron triggers fire within seconds. The Worker becomes the clock; GitHub becomes
the executor via `workflow_dispatch`, which all seven workflows already support.

The workflows keep their own cron blocks as a fallback. Double-firing is
harmless — the duplicate-run guard makes a second morning run a no-op, and
preflight/eod are idempotent.

### Deploy

**1. Create a GitHub token.** github.com -> Settings -> Developer settings ->
Personal access tokens -> Fine-grained tokens -> Generate new token.

- Repository access: **Only select repositories** -> `Claude-Trader-Experiment-`
- Permissions -> Repository permissions -> **Actions: Read and write**
- Expiration: 90 days is fine; the run ends Oct 1

Copy the token. You will not see it again.

**2. Deploy the Worker.**

```bash
cd scheduler
npx wrangler login
npx wrangler deploy
npx wrangler secret put GITHUB_TOKEN     # paste the token
npx wrangler secret put TRIGGER_KEY      # any random string you make up
```

**3. Check it responds.**

```bash
curl https://claude-trader-scheduler.<your-subdomain>.workers.dev/
```

You should get JSON listing the schedule and the current UTC time.

**4. Test a real dispatch** — do this while the market is CLOSED so nothing
trades:

```bash
curl -X POST "https://claude-trader-scheduler.<sub>.workers.dev/run/watchdog.yml?key=<TRIGGER_KEY>"
```

Expect `{"workflow":"watchdog.yml","status":204,"ok":true}`. Then open the
Actions tab — Watchdog should appear within seconds. **That timestamp is the
whole point:** if it starts immediately rather than hours later, the scheduler
works.

**5. Confirm the crons registered.** Cloudflare dashboard -> Workers ->
claude-trader-scheduler -> Settings -> Triggers. Seven cron entries.

### DST

The crons are EDT-based, same as your workflow files. The US switches to EST on
**2026-11-01**, after this run ends on Oct 1 — so you do not need to touch it.
If the experiment extends, add one hour to every UTC time in both
`wrangler.toml` and the `SCHEDULE` map in `worker.js`.

---

## Deploy order

1. Unzip. `run_morning.py`, `config.py`, `selftest.py` go in `dev/`. `scheduler/` sits anywhere.
2. `python selftest.py` — expect **71 PASS**.
3. Commit and push the three Python files.
4. Set up the Worker (Part 2 above).
5. Tomorrow ~8:11am ET, check `logs/health.json`:
   - `scan: OK` with a candidate count -> **working**
   - `scan: LATE` -> the pipeline is healthy but the Worker is not firing; check the Cloudflare trigger list
   - `scan: SKIPPED` -> guard fired for a real reason; read the detail string
   - no morning entry at all -> the run did not happen; check the Actions tab

## Verified

- Whole tree compiles and imports clean
- Late-guard exercised across six clock times
- `worker.js` — `node --check` clean
- selftest section [13] added; **71 PASS**

## Still open

The `TRADES TAKEN` subtitle still reads "ATR brackets · day + swing". Cosmetic,
and wrong now that the engine is day-only.
