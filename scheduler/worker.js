/**
 * claude-trader-scheduler — Cloudflare Worker
 *
 * WHY THIS EXISTS
 * GitHub Actions cron is best-effort and on this repo it has been landing
 * 2-5 hours late, every workflow, every day:
 *
 *   morning    scheduled 12:10 UTC   executed 16:41 UTC   +4h16m
 *   preflight  scheduled 12:10 UTC   executed 17:17 UTC   +4h52m
 *   eod        scheduled 19:50 UTC   executed 22:07 UTC   +2h09m
 *
 * A "morning" scanner that runs at 12:41pm ET is screening pre-market gaps
 * four hours after the open, and an EOD flatten that arrives at 6pm is two
 * hours after the day-TIF brackets already expired. Cloudflare cron triggers
 * fire within seconds, so this Worker becomes the clock and GitHub becomes
 * the executor.
 *
 * The workflows keep their own cron blocks as a fallback. Double-firing is
 * harmless: the duplicate-run guard in run_morning.py makes a second morning
 * run a no-op, and eod/preflight are idempotent.
 */

const OWNER = "ejmacor";
const REPO = "Claude-Trader-Experiment-";

// UTC cron -> workflow file. Times are EDT-based; see the DST note in README.
const SCHEDULE = {
  "10 12 * * 1-5": "morning-run.yml",     //  8:10am ET  scan + enter
  "40 13 * * 1-5": "position-sweep.yml",  //  9:40am ET  stale-position sweep
  "40 16 * * 1-5": "midday-manage.yml",   // 12:40pm ET  manage open positions
  "50 19 * * 1-5": "eod-flatten.yml",     //  3:50pm ET  flatten before the close
  "10 21 * * 1-5": "evening-review.yml",  //  5:10pm ET  outcomes + review
  "30 22 * * 1-5": "watchdog.yml",        //  6:30pm ET  health check
  "40 21 * * 5":   "weekly-review.yml",   //  5:40pm ET Friday
};

async function dispatch(env, workflow) {
  const res = await fetch(
    `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "claude-trader-scheduler",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main" }),
    }
  );
  // 204 No Content is success for this endpoint.
  const ok = res.status === 204;
  const body = ok ? "" : await res.text();
  console.log(
    `[scheduler] ${workflow} -> ${res.status}${ok ? " dispatched" : " FAILED: " + body}`
  );
  return { workflow, status: res.status, ok, body };
}

export default {
  async scheduled(event, env, ctx) {
    const workflow = SCHEDULE[event.cron];
    if (!workflow) {
      console.log(`[scheduler] no workflow mapped to cron ${event.cron}`);
      return;
    }
    ctx.waitUntil(dispatch(env, workflow));
  },

  // Manual trigger + health check:
  //   curl https://<worker>/                     -> shows the schedule
  //   curl -X POST https://<worker>/run/morning-run.yml?key=<TRIGGER_KEY>
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/") {
      return Response.json({
        service: "claude-trader-scheduler",
        repo: `${OWNER}/${REPO}`,
        schedule: SCHEDULE,
        now_utc: new Date().toISOString(),
      });
    }

    const m = url.pathname.match(/^\/run\/([A-Za-z0-9._-]+\.yml)$/);
    if (request.method === "POST" && m) {
      if (!env.TRIGGER_KEY || url.searchParams.get("key") !== env.TRIGGER_KEY) {
        return new Response("unauthorized", { status: 401 });
      }
      if (!Object.values(SCHEDULE).includes(m[1])) {
        return new Response(`unknown workflow: ${m[1]}`, { status: 400 });
      }
      const r = await dispatch(env, m[1]);
      return Response.json(r, { status: r.ok ? 200 : 502 });
    }

    return new Response("not found", { status: 404 });
  },
};
