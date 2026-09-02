/**
 * claude-trader-scheduler — Cloudflare Worker
 *
 * WHY THIS EXISTS
 * GitHub Actions cron is best-effort and on this repo it landed 2-5 hours late,
 * every workflow, every day:
 *
 *   morning    scheduled 12:10 UTC   executed 16:41 UTC   +4h16m
 *   preflight  scheduled 12:10 UTC   executed 17:17 UTC   +4h52m
 *   eod        scheduled 19:50 UTC   executed 22:07 UTC   +2h09m
 *
 * A "morning" scanner running at 12:41pm ET screens pre-market gaps four hours
 * after the open, and an EOD flatten arriving at 6pm is two hours after the
 * day-TIF brackets already expired. Cloudflare cron fires within seconds, so
 * this Worker becomes the clock and GitHub becomes the executor.
 *
 * ONE TRIGGER, NOT SEVEN
 * Cloudflare caps Cron Triggers at 3 per Worker on the free plan (5 on paid).
 * Seven separate triggers will not deploy. So this uses a single every-10-
 * minutes trigger and branches on the UTC clock inside. Every dispatch time
 * below lands on a :10 boundary, so the tick hits all of them exactly.
 *
 * The workflows keep their own cron blocks as a fallback. Double-firing is
 * harmless: the duplicate-run guard makes a second morning run a no-op, and
 * preflight/eod are idempotent.
 */

const OWNER = "ejmacor";
const REPO = "Claude-Trader-Experiment-";

// UTC "HH:MM" -> workflow file. Times are EDT-based; see the DST note below.
// dow: 1-5 = Mon-Fri, 5 = Friday only.
const JOBS = [
  { at: "12:10", dow: [1, 2, 3, 4, 5], wf: "morning-run.yml",    et: "8:10am" },
  { at: "13:40", dow: [1, 2, 3, 4, 5], wf: "position-sweep.yml", et: "9:40am" },
  { at: "16:40", dow: [1, 2, 3, 4, 5], wf: "midday-manage.yml",  et: "12:40pm" },
  { at: "19:50", dow: [1, 2, 3, 4, 5], wf: "eod-flatten.yml",    et: "3:50pm" },
  { at: "21:10", dow: [1, 2, 3, 4, 5], wf: "evening-review.yml", et: "5:10pm" },
  { at: "21:40", dow: [5],             wf: "weekly-review.yml",  et: "5:40pm Fri" },
  { at: "22:30", dow: [1, 2, 3, 4, 5], wf: "watchdog.yml",       et: "6:30pm" },
];

const WORKFLOWS = new Set(JOBS.map(j => j.wf));

async function dispatch(env, workflow, attempts = 3) {
  let last = null;
  for (let i = 1; i <= attempts; i++) {
    try {
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
      if (res.status === 204) {
        console.log(`[scheduler] ${workflow} dispatched (attempt ${i})`);
        return { workflow, status: 204, ok: true, attempts: i };
      }
      last = { workflow, status: res.status, ok: false, body: await res.text() };
      console.log(`[scheduler] ${workflow} attempt ${i} -> ${res.status}: ${last.body}`);
      // 4xx other than rate limiting will not fix themselves; stop retrying.
      if (res.status >= 400 && res.status < 500 && res.status !== 429) break;
    } catch (e) {
      last = { workflow, status: 0, ok: false, body: String(e) };
      console.log(`[scheduler] ${workflow} attempt ${i} threw: ${e}`);
    }
    // Cloudflare does not retry a failed scheduled invocation, so retry here.
    if (i < attempts) await new Promise(r => setTimeout(r, 1500 * i));
  }
  return last || { workflow, status: 0, ok: false, body: "no attempt made" };
}

/** Which job, if any, is due at this UTC instant. Exported shape for testing. */
export function jobFor(date) {
  const hh = String(date.getUTCHours()).padStart(2, "0");
  const mm = String(date.getUTCMinutes()).padStart(2, "0");
  const now = `${hh}:${mm}`;
  const dow = date.getUTCDay(); // 0=Sun .. 6=Sat
  return JOBS.find(j => j.at === now && j.dow.includes(dow)) || null;
}

export default {
  async scheduled(event, env, ctx) {
    const job = jobFor(new Date(event.scheduledTime));
    if (!job) return; // ordinary tick, nothing due
    console.log(`[scheduler] ${job.at} UTC (${job.et} ET) -> ${job.wf}`);
    ctx.waitUntil(dispatch(env, job.wf));
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/") {
      const now = new Date();
      return Response.json({
        service: "claude-trader-scheduler",
        repo: `${OWNER}/${REPO}`,
        now_utc: now.toISOString(),
        next_due: jobFor(now),
        jobs: JOBS,
      }, { headers: { "cache-control": "no-store" } });
    }

    const m = url.pathname.match(/^\/run\/([A-Za-z0-9._-]+\.yml)$/);
    if (request.method === "POST" && m) {
      if (!env.TRIGGER_KEY || url.searchParams.get("key") !== env.TRIGGER_KEY) {
        return new Response("unauthorized", { status: 401 });
      }
      if (!WORKFLOWS.has(m[1])) {
        return new Response(`unknown workflow: ${m[1]}`, { status: 400 });
      }
      const r = await dispatch(env, m[1]);
      return Response.json(r, { status: r.ok ? 200 : 502 });
    }

    return new Response("not found", { status: 404 });
  },
};
