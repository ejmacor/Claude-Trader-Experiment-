"""
selftest.py — verify the fix logic offline, before it touches the account.

Runs with fake Alpaca responses and a temporary working directory. Places no
orders, reads no real account, needs no valid API keys. Run it once after
deploying and any time you change preflight/protection/health.

    python selftest.py

The one thing it cannot verify is Alpaca's actual behaviour on order
submission. Everything upstream of that — staleness rules, stop resolution,
heat arithmetic, health staleness, halt detection — is covered here.
"""

import csv
import os
import sys
import tempfile
from datetime import date, datetime

os.environ.setdefault("ALPACA_API_KEY", "selftest")
os.environ.setdefault("ALPACA_SECRET_KEY", "selftest")

FAILURES = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        expected {want!r}, got {got!r}")
        FAILURES.append(label)


def main():
    repo = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, repo)
    workdir = tempfile.mkdtemp(prefix="clautrader-selftest-")
    os.chdir(workdir)
    os.makedirs("logs", exist_ok=True)

    import config
    import executor
    import health
    import preflight
    import protection_check

    print("\n[1] Stale-position rules")
    # A day-module position from a previous session must be flagged stale.
    stale, _ = preflight.is_stale(
        {"date": "2026-08-11", "module": "DAY_MOMENTUM"}, "2026-09-01")
    check("day position from a prior date is stale", stale, True)

    stale, _ = preflight.is_stale(
        {"date": "2026-09-01", "module": "DAY_MOMENTUM"}, "2026-09-01")
    check("day position opened today is not stale", stale, False)

    stale, _ = preflight.is_stale(
        {"date": "2026-08-28", "module": "SWING_CATALYST"}, "2026-09-01")
    check("swing inside its hold window is not stale", stale, False)

    stale, _ = preflight.is_stale(
        {"date": "2026-07-01", "module": "SWING_CATALYST"}, "2026-09-01")
    check("swing past SWING_MAX_HOLD_DAYS is stale", stale, True)

    stale, _ = preflight.is_stale(
        {"date": "2026-08-11", "module": ""}, "2026-09-01")
    check("unknown-module overnight position is stale", stale, True)

    check("missing row is not stale", preflight.is_stale(None, "2026-09-01")[0], False)

    print("\n[2] Stop resolution from the trade log")
    with open("logs/trade_log.csv", "w", newline="") as f:
        f.write("date,symbol,qty,ref_price,stop,order_id,skipped,module,time_in_force\n")
        f.write("2026-08-07,TWLO,61,238.68,225.92,abc,False,DAY_MOMENTUM,day\n")
        f.write("2026-08-11,FRMI,1775,6.945,6.39,def,False,DAY_MOMENTUM,day\n")
        f.write("2026-08-11,SKIPME,0,1.0,0.9,,True,DAY_MOMENTUM,day\n")
    check("recovers TWLO stop", protection_check.logged_stop_for("TWLO"), 225.92)
    check("recovers FRMI stop", protection_check.logged_stop_for("FRMI"), 6.39)
    check("ignores skipped rows", protection_check.logged_stop_for("SKIPME"), None)
    check("unknown symbol returns None", protection_check.logged_stop_for("NOPE"), None)

    print("\n[3] Heat arithmetic — the deadlock, reproduced and cleared")
    positions = [
        {"symbol": "TWLO", "qty": "61", "avg_entry_price": "238.68", "current_price": "230.00"},
        {"symbol": "NTRA", "qty": "47", "avg_entry_price": "310.905", "current_price": "300.00"},
        {"symbol": "FRMI", "qty": "1775", "avg_entry_price": "6.945", "current_price": "6.60"},
    ]
    executor.get_open_positions = lambda: positions

    # No stops present: every position is charged the 8% ceiling.
    executor.get_open_orders = lambda: []
    heat_unprotected = executor.current_portfolio_heat_pct(94971.0)
    over_cap = heat_unprotected >= config.MAX_PORTFOLIO_HEAT_PCT
    print(f"        unprotected heat = {heat_unprotected:.2f}% "
          f"(cap {config.MAX_PORTFOLIO_HEAT_PCT}%)")
    check("unprotected positions trip the heat cap (the bug)", over_cap, True)

    # Stops restored: heat reports the real entry->stop distance.
    executor.get_open_orders = lambda: [
        {"symbol": "TWLO", "side": "sell", "type": "stop", "stop_price": "225.92", "id": "1"},
        {"symbol": "NTRA", "side": "sell", "type": "stop", "stop_price": "295.47", "id": "2"},
        {"symbol": "FRMI", "side": "sell", "type": "stop", "stop_price": "6.39", "id": "3"},
    ]
    heat_protected = executor.current_portfolio_heat_pct(94971.0)
    print(f"        re-armed heat    = {heat_protected:.2f}%")
    check("re-arming stops brings heat back under the cap",
          heat_protected < config.MAX_PORTFOLIO_HEAT_PCT, True)
    check("re-armed heat is lower than the ceiling fallback",
          heat_protected < heat_unprotected, True)

    print("\n[4] Protection audit classifies correctly")
    executor.get_open_orders = lambda: [
        {"symbol": "TWLO", "side": "sell", "type": "stop", "stop_price": "225.92", "id": "1"},
    ]
    actions = {a["symbol"]: a["action"] for a in protection_check.check(dry_run=True)}
    check("already-stopped position reported protected", actions.get("TWLO"), "protected")
    check("unprotected position queued for re-arm", actions.get("FRMI"), "would_rearm")

    # A position already trading below its stop must be closed, not armed.
    positions.append({"symbol": "BUST", "qty": "10",
                      "avg_entry_price": "100.0", "current_price": "50.0"})
    with open("logs/trade_log.csv", "a", newline="") as f:
        f.write("2026-08-11,BUST,10,100.0,90.0,ghi,False,DAY_MOMENTUM,day\n")
    actions = {a["symbol"]: a["action"] for a in protection_check.check(dry_run=True)}
    check("position through its stop is flagged for closing",
          actions.get("BUST"), "beyond_stop")

    print("\n[5] Health ledger and staleness")
    # Pinned to a fixed calendar. This assertion used to compare against the
    # real wall clock and started failing on 2026-09-02 purely because a day
    # had passed — nothing was broken. Fri 2026-08-28 -> Tue 2026-09-01 is
    # Mon + Tue = 2 weekdays, and it will still be 2 next year.
    check("trading_days_since counts weekdays only (Fri -> Tue)",
          health.trading_days_since("2026-08-28", today=date(2026, 9, 1)), 2)
    check("trading_days_since skips the weekend (Fri -> Mon)",
          health.trading_days_since("2026-08-28", today=date(2026, 8, 31)), 1)
    check("trading_days_since is 0 for the same day",
          health.trading_days_since("2026-08-28", today=date(2026, 8, 28)), 0)
    health.mark("scan", "OK", "3 candidates")
    check("successful stage records last_ok",
          health.read()["stages"]["scan"]["last_ok"] is not None, True)

    health.mark("scan", "HALTED", "Portfolio heat cap reached")
    entry = health.read()["stages"]["scan"]
    check("halt does not advance last_ok", entry["last_ok"], health._today_et())
    check("halt status is recorded", entry["status"], "HALTED")

    # Simulate the real outage: last success three weeks ago, halting daily.
    data = health.read()
    data["stages"]["scan"]["last_ok"] = "2026-08-11"
    import json
    with open(health.HEALTH_JSON, "w") as f:
        json.dump(data, f)
    stale_stages = [s for s, *_ in health.stale(max_trading_days=2)]
    check("watchdog detects a stage that halts daily", "scan" in stale_stages, True)

    print("\n[6] Watchdog catches a workflow that never runs at all")
    import json as _json
    data = health.read()
    data["first_seen"] = "2026-08-01T00:00:00+00:00"
    data["stages"].pop("eod", None)
    with open(health.HEALTH_JSON, "w") as f:
        _json.dump(data, f)
    never = {stage: status for stage, _, status, _ in health.stale()}
    check("a stage with no health record is flagged NEVER_RAN",
          never.get("eod"), "NEVER_RAN")

    print("\n[7] Weekly review distinguishes halted from quiet")
    try:
        import self_review
    except ModuleNotFoundError as e:
        print(f"  SKIP  self_review import unavailable here ({e.name} not installed)")
    else:
        health.mark("scan", "HALTED", "Portfolio heat cap reached")
        check("halt reason surfaced to the reviewer",
              "heat" in self_review._halt_reason().lower(), True)
        health.mark("scan", "OK", "0 candidates")
        check("a genuinely quiet week reports no halt", self_review._halt_reason(), "")

    print("\n[8] Engine coherence — the 2026-07-10 to 2026-09-02 blind spot")
    # For seven weeks the prompt advertised a 1-5 day SWING_CATALYST hold while
    # config.SWING_ENABLED was False. Every swing pick was silently rewritten to
    # a day trade with a day-TIF bracket, so the stop expired at the close and
    # the position rode naked. Nothing in any log said so. These checks fail
    # loudly if the prompt and the executor ever disagree again.
    try:
        import analyst
    except ModuleNotFoundError as e:
        print(f"  SKIP  analyst import unavailable here ({e.name} not installed)")
    else:
      if not hasattr(analyst, "_module_block"):
        # Fail, don't crash: a partially-updated tree must report a clear
        # FAIL rather than a traceback, or the suite gets skipped entirely.
        check("analyst builds its module block from config.SWING_ENABLED",
              False, True)
      else:
        _real = config.SWING_ENABLED
        try:
            config.SWING_ENABLED = False
            block = analyst._module_block()
            check("day-only prompt never offers SWING_CATALYST",
                  "SWING_CATALYST" in block, False)
            check("day-only prompt states the position is flat by the close",
                  "flat by the close" in block.lower(), True)

            config.SWING_ENABLED = True
            check("swing-enabled prompt does offer SWING_CATALYST",
                  "SWING_CATALYST" in analyst._module_block(), True)
        finally:
            config.SWING_ENABLED = _real

        # The prompt must agree with the TIF the executor will actually choose.
        tif = config.SWING_TIME_IN_FORCE if config.SWING_ENABLED else config.DAY_TIME_IN_FORCE
        check("prompt horizon matches the bracket time_in_force",
              (tif == "gtc") == config.SWING_ENABLED, True)

    # An open position is only a swing if it was PLACED as one.
    with open("logs/trade_log.csv", "w", newline="") as f:
        f.write("date,symbol,qty,ref_price,stop,target,order_id,skipped,module,time_in_force\n")
        f.write("2026-08-11,SWNG,10,100.0,90.0,120.0,a,False,SWING_CATALYST,gtc\n")
        f.write("2026-08-11,OVER,10,100.0,90.0,120.0,b,False,DAY_MOMENTUM,day\n")
    import outcomes
    if not hasattr(outcomes, "_logged_module"):
        check("outcomes reads the module a trade was actually placed with",
              False, True)
        outcomes._logged_module = lambda s: "__missing__"
    check("a real swing is labelled OPEN_SWING",
          outcomes._logged_module("SWNG"), "SWING_CATALYST")
    check("a day trade still open is NOT labelled a swing",
          outcomes._logged_module("OVER") == "SWING_CATALYST", False)
    check("an unknown symbol defaults to not-a-swing",
          outcomes._logged_module("NOPE") == "SWING_CATALYST", False)

    print("\n[9] Benchmark gap detection and backfill")
    # benchmark.csv froze at 2026-08-11 and stayed frozen for three weeks
    # because log_spy() could only ever fetch ONE bar (today) and the write sat
    # behind an early exit. A gap, once open, could not be closed by any code
    # in the repo. These checks cover the repair path.
    import benchmark as bm
    bm.BENCHMARK_CSV = "logs/benchmark.csv"

    with open("logs/benchmark.csv", "w", newline="") as f:
        f.write("date,spy_close\n2026-08-11,773.035\n2026-07-06,700.0\n2026-08-10,772.76\n")

    # A TRAILING hole is the shape that actually occurred. A first-to-last
    # scan is blind to it, so this is the check that matters most.
    g = bm.gaps(through="2026-08-14")
    check("gap scan sees the trailing hole after the last row",
          "2026-08-12" in g and "2026-08-13" in g, True)
    check("gap scan excludes weekends",
          any(d in g for d in ("2026-08-08", "2026-08-09")), False)

    _real_fetch = bm.fetch_range
    bm.fetch_range = lambda s_, e_: {"2026-08-10": 999.99,   # already present
                                     "2026-08-12": 774.10,
                                     "2026-08-13": 775.40}
    try:
        added = bm.backfill("2026-08-10", "2026-08-13", dry_run=True)
        check("dry run reports what it would add", added, ["2026-08-12", "2026-08-13"])
        check("dry run writes nothing", len(bm.read_rows()), 3)

        bm.backfill("2026-08-10", "2026-08-13")
        rows = bm.read_rows()
        check("backfill adds the missing rows", len(rows), 5)
        check("backfill never overwrites an existing row", rows["2026-08-10"], "772.76")

        dates = [r.split(",")[0] for r in
                 open("logs/benchmark.csv").read().strip().splitlines()[1:]]
        check("file is written sorted by date", dates, sorted(dates))
        check("no duplicate dates", len(dates), len(set(dates)))
        check("re-running the backfill is a no-op",
              bm.backfill("2026-08-10", "2026-08-13"), [])
    finally:
        bm.fetch_range = _real_fetch

    print("\n[10] Verdict tape staleness (dashboard logic, mirrored)")
    # The tape rendered a 2026-08-11 verdict identically to a same-day one, so
    # FRMI read as the current pick for three weeks while the scanner had not
    # produced a single verdict. The dashboard now marks it; this mirrors the
    # threshold so the two cannot silently drift apart.
    check("a verdict from today is not stale",
          health.trading_days_since("2026-09-02", today=date(2026, 9, 2)) > 1, False)
    check("yesterday's verdict is not stale",
          health.trading_days_since("2026-09-01", today=date(2026, 9, 2)) > 1, False)
    check("a verdict from three weeks ago IS stale",
          health.trading_days_since("2026-08-11", today=date(2026, 9, 2)) > 1, True)
    check("stale age is counted in trading days, not calendar days",
          health.trading_days_since("2026-08-11", today=date(2026, 9, 2)), 16)
    # v1 of the staleness fix marked the same fact in three places — a badge, a
    # dimmed headline and an extra chip — while STILL giving the three-week-old
    # ticker top billing. Labelling something misleading three times does not
    # make it not misleading. The tape now leads with the absence of a verdict
    # and the old pick is collapsed behind a click. These assert the contract
    # the markup depends on, so a future edit cannot quietly promote it back.
    # The suite runs in a tempdir, so resolve the dashboard next to this file.
    _tape_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    _tape = open(_tape_path).read() if os.path.exists(_tape_path) else ""
    check("index.html is readable for the markup checks", bool(_tape), True)
    check("stale tape leads with the absence of a verdict",
          'NO VERDICT SINCE' in _tape, True)
    check("the old verdict is collapsed, not headlined",
          'details class="last-verdict"' in _tape, True)
    check("the three-signal styling is gone", "stale-badge" in _tape, False)
    check("the duplicate not-today chip is gone", "stale-note" in _tape, False)

    print("\n[11] Equity curve integrity")
    # Every stored row is an INTRADAY snapshot. On 2026-07-28 one landed
    # 12.82% below the settled close (84,615.95 vs 97,056.69) with cash
    # unchanged all week. That single row set MAX DRAWDOWN to -16.26%; the
    # real figure is -6.94%. last_equity is the settled close and now wins
    # unconditionally — no threshold, because a 0.25% one flagged eleven
    # perfectly normal rows.
    import trade_logger as tlg
    rows = [{"date": "2026-09-01", "equity": "84615.95", "cash": "50000", "day_pnl_pct": "-12.805"}]
    fixed, corr = tlg.settle_previous_close(list(rows), "2026-09-02", 97056.69)
    check("yesterday's snapshot is replaced by the settled close",
          float(fixed[-1]["equity"]), 97056.69)
    check("the correction is reported", round(corr["drift_pct"], 2), 12.82)
    check("day_pnl_pct derived from the bad snapshot is cleared",
          fixed[-1]["day_pnl_pct"], "")

    # A small, ordinary intraday-vs-close difference is still corrected —
    # the close is authoritative — but it is not treated as an anomaly.
    small = [{"date": "2026-09-01", "equity": "96900.00", "cash": "5", "day_pnl_pct": "0.1"}]
    fx, c2 = tlg.settle_previous_close(list(small), "2026-09-02", 97056.69)
    check("ordinary drift is corrected too", float(fx[-1]["equity"]), 97056.69)
    check("ordinary drift is below the notable threshold",
          c2["drift_pct"] < tlg.EQUITY_DRIFT_NOTABLE_PCT, True)

    # A GAP must never let an old row be overwritten with a much later close.
    gapped = [{"date": "2026-08-11", "equity": "100245.45", "cash": "5", "day_pnl_pct": "0.1"}]
    fg, c3 = tlg.settle_previous_close(list(gapped), "2026-09-02", 97056.69)
    check("a non-adjacent row is left alone", float(fg[-1]["equity"]), 100245.45)
    check("no correction is claimed for a gapped row", c3, None)

    check("max drawdown is driven by the bad row",
          tlg.max_drawdown([{"date": "2026-07-27", "equity": "100000"},
                            {"date": "2026-07-28", "equity": "84615.95"},
                            {"date": "2026-07-29", "equity": "97000"}])[0], -15.38)

    print("\n[12] Closed trades carry their own metadata")
    # SWING_CLOSED rows are the ONLY rows with realized P&L, and they were
    # written with conviction and catalyst_type blank. The judgment readout —
    # the panel that answers whether conviction predicts outcomes — showed
    # `other x8` and `Low (1-6) x8` for three weeks.
    with open("logs/trade_log.csv", "w", newline="") as f:
        f.write("date,symbol,conviction,catalyst_type,qty,ref_price,stop,target,"
                "order_id,skipped,module,time_in_force\n")
        f.write("2026-08-11,ZZZ,8,earnings,10,100.0,90.0,120.0,a,False,DAY_MOMENTUM,day\n")
    check("trade metadata is recoverable by symbol",
          (outcomes._logged_trade("ZZZ").get("conviction"),
           outcomes._logged_trade("ZZZ").get("catalyst_type")), ("8", "earnings"))
    check("an unknown symbol yields no metadata", outcomes._logged_trade("QQQ"), {})

    with open("logs/outcomes.csv", "w", newline="") as f:
        f.write("date,symbol,action,conviction,catalyst_type,reject_reason,"
                "open_to_close_pct,realized_pnl_pct\n")
        f.write("2026-08-20,ZZZ,SWING_CLOSED,,,,,-4.5\n")
    outcomes.repair_metadata(dry_run=True)
    with open("logs/outcomes.csv") as f:
        check("dry run leaves the file untouched", ",,,," in f.read(), True)
    outcomes.repair_metadata()
    with open("logs/outcomes.csv", newline="") as f:
        row = list(csv.DictReader(f))[0]
    check("repair backfills conviction", row["conviction"], "8")
    check("repair backfills catalyst_type", row["catalyst_type"], "earnings")
    check("repair is idempotent", outcomes.repair_metadata(), [])

    print("\n[13] Late-run entry guard")
    # GitHub Actions cron has been landing 2-5 hours late on every workflow.
    # The scanner screens PRE-MARKET gaps; acting on that read at lunchtime is
    # a different, untested strategy wearing the same name. Past the cutoff the
    # run still remediates and logs — it just places no new entries, loudly.
    from zoneinfo import ZoneInfo as _Z

    def _entries_allowed(hhmm, cutoff=None):
        cutoff = cutoff or config.MAX_ENTRY_TIME_ET
        h, m = (int(x) for x in hhmm.split(":"))
        now = datetime(2026, 9, 3, h, m, tzinfo=_Z("America/New_York"))
        ch, cm = (int(x) for x in cutoff.split(":"))
        return now <= now.replace(hour=ch, minute=cm, second=0, microsecond=0)

    check("an on-time 8:10am run may trade", _entries_allowed("08:10"), True)
    check("a slightly late 9:45am run may still trade", _entries_allowed("09:45"), True)
    check("the cutoff minute itself is allowed",
          _entries_allowed(config.MAX_ENTRY_TIME_ET), True)
    check("one minute past the cutoff blocks entries", _entries_allowed("10:31"), False)
    check("the observed 12:41pm late run blocks entries",
          _entries_allowed("12:41"), False)
    check("cutoff is before the pre-market screen goes stale mid-session",
          config.MAX_ENTRY_TIME_ET < "12:00", True)

    print("\n[14] A blocked day still says something true about the market")
    # The late path used to write "[ENGINE: no entries — past cutoff]" as the
    # day's market note, and the guardrail-halt path wrote nothing to
    # decisions.jsonl at all — leaving the verdict tape frozen on the last good
    # day, which is precisely the state that hid the August outage. Both now
    # log a real row led by live regime data.
    try:
        import run_morning as _rm
    except ModuleNotFoundError as e:
        print(f"  SKIP  run_morning import unavailable here ({e.name} not installed)")
    else:
        _reg = {"regime": "BULL_QUIET", "risk_mult": 1.0,
                "detail": {"spy": 765.94, "sma50": 754.29, "sma200": 710.23,
                           "realized_vol_20d_pct": 9.3}}
        note = _rm.regime_note(_reg)
        check("an unblocked note names the regime", "BULL_QUIET" in note, True)
        check("an unblocked note carries live SPY levels", "765.94" in note, True)
        check("an unblocked note carries realized vol", "9.3%" in note, True)
        check("an unblocked note claims no halt", "No trades today" in note, False)

        blocked = _rm.regime_note(_reg, "past the 10:30 entry cutoff", 3)
        check("a blocked note still leads with the market read",
              blocked.startswith("BULL_QUIET regime"), True)
        check("a blocked note says how many were screened",
              "3 candidate(s) screened" in blocked, True)
        check("a blocked note gives the reason",
              "past the 10:30 entry cutoff" in blocked, True)
        check("the bare engine complaint is gone", "[ENGINE:" in blocked, False)

        # Must never raise when the regime lookup itself failed.
        check("a missing regime degrades instead of crashing",
              _rm.regime_note(None, "past cutoff", 0).startswith("UNKNOWN regime"), True)

        # Below-200d wording, so a BEAR tape is not described as constructive.
        _bear = {"regime": "BEAR", "detail": {"spy": 600.0, "sma50": 700.0,
                                              "sma200": 710.0, "realized_vol_20d_pct": 24.1}}
        check("a bear tape is described as below its 200d SMA",
              "below its 200d SMA" in _rm.regime_note(_bear), True)

    print("\n[15] Benchmark self-heal reaches interior gaps")
    # The 2026-09-02 run detected 26 missing weekdays and filled 16. The other
    # ten (Jul 14, Jul 27-31, Aug 3-6) sit BEFORE the last logged row, and a
    # no-arg backfill() starts at max(rows)+1 — so it can only ever close a
    # trailing hole. gaps() already knows the earliest one; pass it.
    with open("logs/benchmark.csv", "w", newline="") as f:
        f.write("date,spy_close\n")
        for d, v in [("2026-07-06", 700.0), ("2026-07-13", 710.0),
                     ("2026-07-16", 715.0), ("2026-09-01", 765.0)]:
            f.write(f"{d},{v}\n")
    _g = bm.gaps(through="2026-09-02")
    check("interior gaps are detected", "2026-07-14" in _g, True)
    check("trailing gaps are detected", "2026-09-02" in _g, True)
    check("the earliest gap precedes the last logged row",
          _g[0] < max(bm.read_rows()), True)

    _asked = {}
    _real = bm.fetch_range
    bm.fetch_range = lambda s_, e_: (_asked.update(start=s_, end=e_) or {})
    try:
        bm.backfill()                       # the buggy call
        check("a no-arg backfill starts after the last row — misses interiors",
              _asked["start"] > _g[0], True)
        bm.backfill(start=_g[0])            # the fixed call
        check("passing the earliest gap covers the interior holes",
              _asked["start"], _g[0])
    finally:
        bm.fetch_range = _real

    print("\n[16] No-trade days are split by cause")
    # Lumping "the scanner judged and declined" together with "the engine
    # blocked entries" is how three weeks of outage read as discipline. The
    # engine stamps its own blocks into market_note; the dashboard keys off it.
    import re as _re
    _blocked = lambda note: bool(_re.search(r"No trades today —|\[ENGINE:", note or ""))
    check("a late-run block is counted as blocked",
          _blocked("BULL_QUIET regime with SPY above both SMAs. No trades today "
                   "— 2 candidate(s) screened but not traded: past the cutoff."), True)
    check("the older bare engine note is still recognised",
          _blocked("[ENGINE: no entries — run started 15:56 ET, past the 10:30 "
                   "entry cutoff]"), True)
    check("a guardrail halt is counted as blocked",
          _blocked("BULL_QUIET regime. No trades today — no entries: risk "
                   "guardrail: portfolio heat 3.5% at cap."), True)
    check("a genuine judgment call is NOT counted as blocked",
          _blocked("BULL_QUIET regime with SPY well above both SMAs — a "
                   "constructive tape, but no candidate cleared the screen."), False)
    check("an empty note is not blocked", _blocked(""), False)

    print()
    print("=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED: {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
