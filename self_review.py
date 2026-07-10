"""
self_review.py — Claude reviews its own week.

Runs Friday evenings. Reads the week's decisions, trades, and outcomes
(including how REJECTED stocks fared), sends them to Claude in a
reviewer role, and writes a markdown post-mortem to journal/ — which
the dashboard renders in the Analyst Journal panel.

HARD RULE: this analysis is write-only. It is never fed back into the
morning trading prompt. The 90-day rules stay frozen; this is a
report card, not a coach.
"""

import csv
import glob
import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic

ET = ZoneInfo("America/New_York")
MODEL = "claude-sonnet-4-6"
JOURNAL_DIR = "journal"

WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+?)(?:\|[^\[\]]*)?\]\]")


def today_et():
    return datetime.now(ET).date()


def week_window():
    """Monday..Friday (ET) of the current trading week.

    End is clamped to Friday so a run that drifts into Saturday (GitHub
    cron delay) reviews the same window and writes the same filename —
    this is what previously produced duplicate 07-03 + 07-04 reviews.
    """
    today = today_et()
    start = today - timedelta(days=today.weekday())          # Monday
    friday = start + timedelta(days=4)
    end = min(today, friday)
    return start.isoformat(), end.isoformat(), friday.isoformat()


def existing_graph():
    """Inventory of every wikilink node already in the journal, by type,
    plus each prior review's Threads line. This is the graph's memory:
    without it the reviewer invents fresh pattern names every week and
    the brain map fragments instead of growing heavier on repeats.

    Write-only boundary intact: only the REVIEWER sees this; the trading
    model never reads the journal.
    """
    nodes = {"pattern": set(), "catalyst": set(), "ticker": set(),
             "miss": set(), "call": set(), "other": set()}
    threads = []
    for path in sorted(glob.glob(f"{JOURNAL_DIR}/*.md")):
        try:
            with open(path) as f:
                text = f.read()
        except OSError:
            continue
        for m in WIKILINK_RE.finditer(text):
            node = m.group(1).strip()
            kind = node.split("-", 1)[0] if "-" in node else "other"
            nodes.get(kind, nodes["other"]).add(node)
        for line in text.splitlines():
            if line.strip().lower().startswith(("threads", "**threads")):
                threads.append(f"{os.path.basename(path)}: {line.strip()}")
    return {k: sorted(v) for k, v in nodes.items() if v}, threads[-6:]


def load_jsonl(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def load_csv(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def in_window(datestr, start, end):
    return bool(datestr) and start <= datestr <= end


def et_date_of(ts):
    try:
        return datetime.fromisoformat(ts).astimezone(ET).date().isoformat()
    except (ValueError, TypeError):
        return ""


def main():
    start, end, friday = week_window()

    # Idempotency: one review per trading week, anchored to its Friday.
    fname = f"{JOURNAL_DIR}/{friday}-weekly-self-review.md"
    if os.path.exists(fname) and not os.environ.get("FORCE_REVIEW"):
        print(f"{fname} already exists — one review per week. Skipping.")
        return

    # --- gather the week's record ---
    decisions_raw = load_jsonl("logs/decisions.jsonl")
    by_date = {}
    for row in decisions_raw:
        d = et_date_of(row.get("timestamp", ""))
        if in_window(d, start, end):
            by_date[d] = row  # last run per date wins
    week_decisions = [
        {"date": d,
         "candidates": [
             {"symbol": c["symbol"], "gap_pct": c.get("gap_pct"),
              "headlines": c.get("headlines", [])[:3]}
             for c in row.get("candidates_sent", [])],
         "decision": row.get("decision", {})}
        for d, row in sorted(by_date.items())
    ]

    week_trades = [t for t in load_csv("logs/trade_log.csv")
                   if in_window(t.get("date", ""), start, end)]
    outcomes_all = load_csv("logs/outcomes.csv")
    week_outcomes = [o for o in outcomes_all
                     if in_window(o.get("date", ""), start, end)]
    equity = load_csv("logs/equity_curve.csv")
    eq_now = equity[-1]["equity"] if equity else "100000"

    if not week_decisions:
        print("No decisions this week; skipping review.")
        return

    cumulative = {
        "total_taken": sum(1 for o in outcomes_all if o.get("action") == "TAKEN"),
        "total_rejected": sum(1 for o in outcomes_all if o.get("action") == "REJECTED"),
        "equity": eq_now,
    }

    graph_nodes, prior_threads = existing_graph()
    payload = {
        "week": {"start": start, "end": end},
        "daily_decisions": week_decisions,
        "trades_placed": week_trades,
        "outcomes_this_week": week_outcomes,
        "cumulative": cumulative,
        "existing_graph_nodes": graph_nodes,
        "prior_review_threads": prior_threads,
    }

    system = """You are the weekly reviewer for an autonomous paper-trading
experiment in which an AI (you, in a different role) judges pre-market news
catalysts and trades them long-only with fixed 4%/8% brackets. You are
reviewing YOUR OWN decisions from this week with the detachment of a senior
desk risk reviewer. Write a markdown post-mortem for the experiment journal.

Requirements:
- Be specific: reference actual tickers, stated reasoning, and outcomes.
- Grade the judgment, not the luck: a trade can lose with sound reasoning
  and win with bad reasoning. Say which happened.
- Review the REJECTIONS against what those stocks then did (open-to-close):
  name the best call and the worst miss of the week.
- Assess conviction calibration if there are enough trades to say anything.
- Be brutally honest about sample size. Never present 3 trades as a pattern.
- You may note hypotheses worth testing in v2, clearly labeled as such.
- NEVER suggest changing the frozen rules mid-experiment.
- Length: 250-450 words. Plain, direct, no hype. Start with a one-line
  verdict on the week. Use markdown headers sparingly (## max).
- Do not use tables. Do not include a title line; the journal adds one.

KNOWLEDGE GRAPH (Obsidian wikilinks) — weave these into the prose:
- Every ticker discussed: [[ticker-XXXX]] (e.g. [[ticker-CLRO]])
- Every catalyst type touched: [[catalyst-earnings]], [[catalyst-fda]],
  [[catalyst-ma]], [[catalyst-contract]], [[catalyst-none]], etc.
- Every recurring behavior you identify, good or bad, as a pattern node
  with a short kebab-case name, e.g. [[pattern-already-run-gaps]],
  [[pattern-holiday-tape-junk]], [[pattern-overweighting-headline-size]].
  The payload's `existing_graph_nodes` lists EVERY node already in the
  graph and `prior_review_threads` shows what past reviews flagged.
  You MUST reuse those exact node names when the same behavior, ticker,
  or catalyst recurs — repetition is what makes recurring behaviors grow
  heavy in the graph. Invent a new pattern node only for a genuinely new
  behavior, and when you do, also link it to at least one existing node
  in prose so the graph stays connected rather than fragmenting.
- Significant single events: [[miss-YYYY-MM-DD-TICKER]] for a costly
  wrong call, [[call-YYYY-MM-DD-TICKER]] for a notably right one.
- End with a "Threads" line listing the 3-6 most important links from
  this review."""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content":
                   "Here is this week's complete record. Write the post-mortem.\n\n"
                   + json.dumps(payload, indent=2, default=str)}],
    )
    review = "".join(b.text for b in resp.content if b.type == "text").strip()

    os.makedirs(JOURNAL_DIR, exist_ok=True)
    with open(fname, "w") as f:
        f.write(f"*Machine-written post-mortem — Claude reviewing its own "
                f"week of {start} to {end}. Write-only: the trading model "
                f"never reads this.*\n\n{review}\n")

    print(f"Review written to {fname}")
    # First ~300 chars for the phone notification
    print("SUMMARY_START")
    print(review[:300].replace("\n", " "))
    print("SUMMARY_END")


if __name__ == "__main__":
    main()
