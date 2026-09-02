"""
run_morning.py — v2 daily entry point (8:30am ET via GitHub Actions).

Pipeline: guards -> regime -> scan -> Claude analyzes -> risk gate (BLOCKING)
          -> challenger (advisory) -> ATR bracket orders -> log.
"""

import sys
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import requests

import analyst
import benchmark
import challenger
import config
import executor
import health
import preflight
import regime as regime_mod
import scanner
import shadow_gate
import trade_logger


# Order statuses that represent an order which never reached the market.
# A rejected or immediately-cancelled order is not evidence the day already ran.
DEAD_ORDER_STATUSES = {
    "canceled", "cancelled", "expired", "rejected", "suspended",
    "pending_cancel", "pending_replace", "replaced", "stopped",
}


def already_ran_today():
    """Duplicate-run guard: True if an ENTRY order was already placed today (ET).

    THE BUG THIS FIXES (2026-09-02): the original version counted *any* order
    returned by /v2/orders — including the sells that EOD flatten, preflight
    remediation and stop re-arms submit. So the moment any housekeeping
    workflow touched the order endpoint on a given day, the scanner was locked
    out for the rest of that day. `scan` was marked SKIPPED before regime,
    before candidates, before the analyst ran, and no verdict was ever written.
    The decision wire froze while every other panel kept updating.

    An entry, for this system, is a BUY that actually reached the market and is
    not a close/cover of a position we were already holding when the run began.
    Exits are sells; housekeeping is sells; neither means "today already ran".
    """
    et_midnight = datetime.combine(
        datetime.now(ZoneInfo("America/New_York")).date(), time.min,
        tzinfo=ZoneInfo("America/New_York"),
    ).astimezone(timezone.utc)
    resp = requests.get(
        f"{executor.BASE_URL}/v2/orders",
        headers=executor.HEADERS,
        params={"status": "all", "after": et_midnight.isoformat(), "limit": 100},
        timeout=30,
    )
    resp.raise_for_status()
    # Orders from prior days shouldn't trip the guard — only orders SUBMITTED
    # today (ET) count. Timestamps from Alpaca are UTC; convert before comparing.
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()

    def _et_date(ts):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(
                ZoneInfo("America/New_York")).date().isoformat()
        except (ValueError, TypeError, AttributeError):
            return ""

    # Symbols we were already holding. A buy in one of these is an add or a
    # short cover, not a fresh entry from today's scan.
    try:
        held = {p.get("symbol") for p in executor.get_open_positions()}
    except Exception as e:  # noqa: BLE001
        print(f"      Guard could not read positions ({e}) — not treating any buy as a cover.")
        held = set()

    entries = []
    for o in resp.json():
        if not (_et_date(o.get("submitted_at")) == today or
                _et_date(o.get("created_at")) == today):
            continue
        if str(o.get("side", "")).lower() != "buy":
            continue
        if str(o.get("status", "")).lower() in DEAD_ORDER_STATUSES:
            continue
        if o.get("symbol") in held:
            continue
        entries.append(o.get("symbol"))

    if entries:
        print(f"      Entry orders already submitted today: {sorted(set(entries))}")
    return bool(entries)


def main():
    print("=" * 60)
    print(f"CLAUDE TRADER v{config.CONFIG_VERSION} — morning run")
    print("=" * 60)

    health.mark("morning", "OK", "run started")

    # 0a. Duplicate-run guard
    #     Only ENTRY orders count. Exits, flattens and stop re-arms must never
    #     lock the scanner out — that is what killed the wire from 2026-08-12.
    try:
        duplicate = already_ran_today()
    except Exception as e:  # noqa: BLE001
        # A guard that cannot check must not silently halt the pipeline. Fail
        # open and say so — a duplicate entry is caught downstream by the
        # position check; a false halt is invisible and costs a whole day.
        print(f"      Duplicate-run guard errored ({e}) — failing OPEN and continuing.")
        health.mark("scan", "WARN", f"duplicate-run guard unavailable: {e}")
        duplicate = False
    if duplicate:
        print("Entry orders already submitted today — duplicate run detected. Exiting.")
        health.mark("scan", "SKIPPED", "duplicate run guard — entry orders already placed today")
        sys.exit(0)

    # 0b. Regime first — it feeds both guardrails and the analyst
    print("\n[1/6] Classifying market regime...")
    regime = regime_mod.classify()
    print(f"      {regime['regime']} (risk x{regime['risk_mult']}) | {regime['detail']}")

    # 0c. PREFLIGHT — remediation BEFORE judgement.
    #
    # This ordering is deliberate and it is the fix for the 2026-08-12
    # deadlock. Guardrails used to run first and exit on failure, which meant
    # stale unprotected positions inflated portfolio heat, heat tripped the
    # guardrail, and the guardrail exited before any code that could have
    # cleaned up those positions. The system could not reach its own remedy.
    #
    # Preflight re-arms missing stops and closes positions that outlived
    # their hold. It relaxes NO risk parameter — guardrails below still have
    # absolute veto over new trades.
    print("\n[2/6] Preflight: protective stops and stale inventory...")
    try:
        pre = preflight.run()
        print(f"      {pre.get('summary', 'preflight complete')}")
    except Exception as e:  # noqa: BLE001 — preflight must never kill the run
        print(f"      Preflight error (continuing to guardrails): {e}")
        health.mark("preflight", "ERROR", str(e))

    # 0d. Benchmark BEFORE the guardrail exit. benchmark.csv froze on
    # 2026-08-11 for the same reason the scanner did — it was logged after the
    # analyst, so a halt took the SPY comparison line down with it. The
    # benchmark is market data; it has nothing to do with whether we may trade.
    try:
        benchmark.log_spy()
    except Exception as e:  # noqa: BLE001
        print(f"      Benchmark log error (ignored): {e}")

    # 0e. Risk guardrails — evaluated AFTER remediation, on a truthful account
    account = executor.get_account()
    ok, reason = executor.guardrails_pass(account, regime)
    trade_logger.log_equity(account)
    if not ok:
        print(f"HALTED, no trades today: {reason}")
        # A halt is a real outcome, not silence. Recording it here is what
        # lets the dashboard and the weekly review know the difference
        # between "looked and found nothing" and "never looked".
        health.mark("scan", "HALTED", reason)
        health.mark("execute", "HALTED", reason)
        sys.exit(0)

    # 1. Scan
    print("\n[3/6] Scanning pre-market gappers...")
    candidates = scanner.build_candidates()
    print(f"      {len(candidates)} candidates with catalysts")
    # The scanner ran. Zero candidates is a legitimate result and reports OK;
    # only never reaching this line is a failure.
    health.mark("scan", "OK", f"{len(candidates)} candidate(s) after filters")
    for c in candidates:
        t = c["technicals"]
        print(f"      {c['symbol']:6s} +{c['gap_pct']}% | rvol {t['relative_volume']} | atr {t['atr_pct']}% | {c['news'][0]['headline'][:60]}")

    # 2. Analyze
    print("\n[4/6] Claude catalyst analysis...")
    decision = analyst.analyze(candidates, regime)
    print(f"      Market note: {decision.get('market_note', '')}")
    print(f"      Trades proposed: {len(decision['trades'])}")

    # 2b. Risk gate — BLOCKING in v2 (was shadow in v1)
    try:
        gate = shadow_gate.evaluate(
            candidates, decision,
            open_position_count=len(executor.get_open_positions()),
        )
        vetoed = {e["symbol"] for e in gate["evaluated"] if e["verdict"] == "WOULD_VETO"}
        if config.GATE_BLOCKING and vetoed:
            kept = [t for t in decision["trades"] if t["symbol"] not in vetoed]
            for sym in vetoed:
                flags = next((e["flags"] for e in gate["evaluated"] if e["symbol"] == sym), [])
                reason = "; ".join(f["detail"] for f in flags if f["level"] == "veto")
                decision.setdefault("rejected", []).append({"symbol": sym, "reason": f"GATE VETO: {reason}"})
                print(f"      GATE VETO {sym}: {reason}")
            decision["trades"] = kept
        s = gate["summary"]
        print(f"      Gate ({'BLOCKING' if config.GATE_BLOCKING else 'shadow'}): "
              f"{s['would_allow']} allow / {s['flagged']} flagged / {s['would_veto']} vetoed")
    except Exception as e:  # noqa: BLE001 — gate crash must never stop the run
        print(f"      Gate error (ignored): {e}")

    trade_logger.log_decision(candidates, decision)

    # 2c. Challenger — second Claude call as risk officer (advisory)
    try:
        chall = challenger.review(candidates, decision)
        verdicts = {r["symbol"]: r["verdict"] for r in chall.get("reviews", [])}
        if verdicts:
            print("      Challenger:", ", ".join(f"{k}={v}" for k, v in verdicts.items()), "(advisory)")
    except Exception as e:  # noqa: BLE001
        print(f"      Challenger error (ignored): {e}")

    # 3. Execute
    print("\n[5/6] Placing ATR bracket orders (PAPER)...")
    by_symbol = {c["symbol"]: c for c in candidates}
    for trade in decision["trades"]:
        sym = trade["symbol"]
        cand = by_symbol.get(sym)
        if cand is None:
            execution = {"skipped": True, "reason": "symbol not in candidate list (hallucination guard)"}
        else:
            try:
                execution = executor.place_bracket(
                    sym, cand["last_price"],
                    atr=cand["technicals"].get("atr"),
                    module=trade.get("module", "DAY_MOMENTUM"),
                    regime_mult=regime["risk_mult"],
                )
            except Exception as e:  # noqa: BLE001
                execution = {"skipped": True, "reason": f"order error: {e}"}
        trade_logger.log_trade(trade, execution)
        status = ("SKIPPED: " + execution.get("reason", "")) if execution.get("skipped") else \
            (f"BUY {execution['qty']} @ ~{execution['ref_price']} | stop {execution['stop']} | "
             f"target {execution['target']} | {execution['module']} ({execution['time_in_force']})"
             + ("" if execution.get("verified") else " | UNVERIFIED"))
        print(f"      {sym:6s} score {trade.get('setup_score')}/10 -> {status}")

    placed = sum(1 for t in decision["trades"] if t.get("symbol"))
    health.mark("execute", "OK", f"{placed} trade(s) processed from {len(candidates)} candidate(s)")

    print("\n[6/6] Run complete. Day-only mode: the 3:50pm ET flatten job is the"
          " primary no-overnight guarantee; preflight is the backstop if it fails.")


if __name__ == "__main__":
    main()
