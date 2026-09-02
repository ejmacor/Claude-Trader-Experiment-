"""
health.py — pipeline liveness ledger.

The failure this exists to prevent: on 2026-08-12 the morning run began
exiting at the guardrail check before the scanner ever ran. The equity curve
and regime log kept updating (both are written BEFORE that exit), so the
dashboard stayed green for three weeks while the trading pipeline was dead.

Liveness != function. Every stage now reports whether it actually did its
job, and the dashboard reads this file to say so out loud.

Stages: morning, scan, execute, midday, eod, evening, weekly_review
Status:  OK | HALTED | SKIPPED | ERROR

Written to logs/health.json (one object, rewritten each time — not a log).
"""

import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HEALTH_JSON = "logs/health.json"

STAGES = ["morning", "preflight", "scan", "execute", "midday", "eod", "evening", "weekly_review"]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _today_et():
    return datetime.now(ET).date().isoformat()


def _file_age_days():
    """How long the health ledger has existed. Used to avoid alerting on
    stages that simply have not had their first scheduled slot yet."""
    try:
        first = read().get("first_seen")
        if not first:
            return 0
        start = datetime.fromisoformat(first).date()
        return (datetime.now(ET).date() - start).days
    except Exception:  # noqa: BLE001
        return 0


def _ledger_age_trading_days():
    """Ledger age in Mon-Fri days. A stage cannot be N days stale if the
    ledger recording it is younger than N days."""
    try:
        first = read().get("first_seen")
        if not first:
            return 0
        return trading_days_since(datetime.fromisoformat(first).astimezone(ET).date().isoformat())
    except Exception:  # noqa: BLE001
        return 0


def read():
    """Current health object. Never raises — a missing/corrupt file is empty."""
    try:
        with open(HEALTH_JSON) as f:
            data = json.load(f)
        if isinstance(data, dict) and "stages" in data:
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"updated": None, "stages": {}}


def mark(stage, status, detail=""):
    """Record the outcome of a pipeline stage.

    last_ok only advances on status == "OK", which is the whole point: a
    stage that runs every day and fails every day must not look fresh.
    """
    os.makedirs("logs", exist_ok=True)
    data = read()
    prev = data["stages"].get(stage, {})
    entry = {
        "status": status,
        "detail": str(detail)[:400],
        "last_run": _now(),
        "last_run_date_et": _today_et(),
        "last_ok": _today_et() if status == "OK" else prev.get("last_ok"),
    }
    data["stages"][stage] = entry
    data["updated"] = _now()
    data.setdefault("first_seen", _now())
    try:
        with open(HEALTH_JSON, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:  # noqa: BLE001
        print(f"[health] could not write {HEALTH_JSON}: {e}")
    return entry


def trading_days_since(date_str):
    """Rough Mon-Fri day count since an ISO date. Holidays are not excluded —
    this feeds an alert threshold, not an accounting record."""
    if not date_str:
        return 999
    try:
        start = datetime.fromisoformat(date_str).date()
    except (ValueError, TypeError):
        return 999
    today = datetime.now(ET).date()
    days = 0
    cur = start
    while cur < today:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


def stale(max_trading_days=2):
    """Stages whose last SUCCESSFUL run is older than the threshold.

    Returns [(stage, days_since_ok, status, detail)] for the watchdog.
    weekly_review gets a longer leash — it only runs on Fridays.
    """
    out = []
    data = read()
    for stage in STAGES:
        entry = data["stages"].get(stage)
        limit = 7 if stage == "weekly_review" else max_trading_days
        if entry is None:
            # A stage that has NEVER reported is the exact failure mode that
            # caused this outage: eod-flatten and midday-manage stopped
            # executing entirely, so they could not even file an error. An
            # absent stage must alert, not be skipped — but not before its
            # first scheduled slot has plausibly passed.
            if _file_age_days() >= 2:
                out.append((stage, 999, "NEVER_RAN",
                            "no health record — is this workflow enabled?"))
            continue
        days = trading_days_since(entry.get("last_ok"))
        # COLD START (fixed 2026-09-02): a stage with last_ok == None reports
        # 999 days stale. On a ledger created yesterday that is nonsense — it
        # made the dashboard shout "PIPELINE HALTED" at stages that were in
        # fact running fine, just without a recorded success yet. A stage
        # cannot be staler than the ledger that records it.
        if not entry.get("last_ok"):
            days = min(days, _ledger_age_trading_days())
        if days > limit:
            out.append((stage, days, entry.get("status", "?"), entry.get("detail", "")))
    return out


def summary_line():
    rows = stale()
    if not rows:
        return "All pipeline stages reporting OK."
    return " | ".join(f"{s}: {d}d stale ({st})" for s, d, st, _ in rows)


if __name__ == "__main__":
    print(json.dumps(read(), indent=2))
    print()
    print(summary_line())
