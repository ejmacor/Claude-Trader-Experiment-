# Getting it trading — the last step, 2026-09-02

```
scheduler/worker.js
scheduler/wrangler.toml
```

**All the Python and dashboard code is already deployed and verified on `main`.**
The only remaining data gap is the ten benchmark rows, which tomorrow's morning
run closes by itself. This is the last piece.

---

## Correction to what I sent earlier

The `scheduler/` in the get-it-trading zip had **seven** cron triggers in
`wrangler.toml`. Cloudflare caps Cron Triggers at **3 per Worker on the free
plan** (5 on paid). That deploy would have failed with a limit error.

This version uses **one** trigger and multiplexes the seven jobs inside the
Worker, branching on the UTC clock. If you already unzipped the old
`scheduler/`, replace both files.

```toml
crons = ["*/10 12-23 * * 1-5"]
```

Every dispatch time lands on a `:10` boundary, so a ten-minute tick hits all of
them exactly. Simulated a full week: 31 dispatches, correct counts, nothing on
weekends, weekly-review on Friday only.

```
morning-run.yml        5     Saturday 12:10        -> nothing
position-sweep.yml     5     Monday 12:20 (no job) -> nothing
midday-manage.yml      5     Friday 21:40          -> weekly-review.yml
eod-flatten.yml        5     Thursday 21:40        -> nothing
evening-review.yml     5
watchdog.yml           5
weekly-review.yml      1
```

Also added: **retry on failure.** Cloudflare does not retry a failed scheduled
invocation — if it throws, that run is gone until the next tick. `dispatch()`
now retries up to 3 times with backoff, and stops early on a 4xx that will not
fix itself.

---

## Deploy

### 1. GitHub token

github.com -> Settings -> Developer settings -> Personal access tokens ->
**Fine-grained tokens** -> Generate new token.

- Repository access: **Only select repositories** -> `Claude-Trader-Experiment-`
- Permissions -> Repository permissions -> **Actions: Read and write**
- Expiration: 90 days is plenty; the run ends Oct 1

Copy the token now. GitHub will not show it again.

### 2. Deploy the Worker

```bash
cd scheduler
npx wrangler login
npx wrangler deploy
npx wrangler secret put GITHUB_TOKEN     # paste the token
npx wrangler secret put TRIGGER_KEY      # any random string you invent
```

This creates a **second, separate** Worker. It does not touch
`claude-trader-live`.

### 3. Checkpoint — is it alive?

```bash
curl https://claude-trader-scheduler.<your-subdomain>.workers.dev/
```

Expect JSON with `now_utc`, `next_due`, and all seven jobs listed. If you get a
404 or a Cloudflare error page, the deploy did not take.

### 4. Checkpoint — does dispatch work?

Market is closed, so this is safe:

```bash
curl -X POST "https://claude-trader-scheduler.<sub>.workers.dev/run/watchdog.yml?key=<TRIGGER_KEY>"
```

Expect `{"workflow":"watchdog.yml","status":204,"ok":true,"attempts":1}`.

- `401` -> TRIGGER_KEY does not match
- `502` with a 404 body -> the GITHUB_TOKEN cannot see the repo; check the token's repository access
- `502` with a 403 body -> the token is missing **Actions: Read and write**

Then open the Actions tab. Watchdog should appear **within seconds**. That
timestamp is the entire point of this exercise — if it starts immediately
rather than hours later, the scheduler works.

### 5. Checkpoint — is the cron registered?

Cloudflare dashboard -> Workers & Pages -> `claude-trader-scheduler` ->
Settings -> Triggers. You should see **one** cron entry: `*/10 12-23 * * 1-5`.

If you see none, `wrangler deploy` did not pick up `[triggers]` — re-run it from
inside the `scheduler/` directory.

---

## Tomorrow, 8:10am ET

Check `logs/health.json`:

| `scan` says | means |
|---|---|
| `OK` + candidate count | **working** — scheduler fired, guard passed, scanner ran |
| `LATE` | pipeline healthy, Worker did not fire — check step 5 |
| `SKIPPED` | read the detail. "entry orders already placed today" is the guard firing legitimately |
| stage absent | the run never happened — check the Actions tab |

Also expect: the last ten benchmark gaps closed (`python benchmark.py --check`
reports 0), a real analyst market note on the tape instead of an engine line,
and the no-trade subtitle back to plain "discipline, not absence".

Friday: the weekly self-review runs for the first time since Aug 14, and the
brain map gets new nodes.

---

## DST

Everything here is EDT-based. The US switches to EST on **2026-11-01**, after
the run ends Oct 1 — so you do not need to touch it. If you extend, add one
hour to each `at:` in the `JOBS` array in `worker.js`. The cron window
`12-23` is wide enough that it does not need changing.

## Verified

- `worker.js` — `node --check` clean
- `jobFor()` simulated across a full week of ten-minute ticks: 31 dispatches, correct per-workflow counts, nothing on weekends or off-times, weekly-review Friday only
- Confirmed every job time is reachable by a `*/10` tick
