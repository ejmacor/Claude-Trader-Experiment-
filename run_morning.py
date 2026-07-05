"""
run_morning.py — The daily entry point. Run this at 8:30am ET each
trading day (manually or via scheduler).

Pipeline: guards -> scan -> Claude analyzes -> bracket orders -> log.
"""

import sys
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import requests

import analyst
import benchmark
import challenger
import executor
import scanner
import shadow_gate
import trade_logger


def already_ran_today():
    """Duplicate-run guard: True if ANY order was already placed today (ET).

    Protects against double-trading if the workflow runs twice in one
    morning (scheduled run + manual trigger, or a re-run)."""
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
    return len(resp.json()) > 0


def main():
    print("=" * 60)
    print("CLAUDE TRADER — morning run")
    print("=" * 60)

    # 0a. Duplicate-run guard — never trade twice in one day.
    if already_ran_today():
        print("Orders already exist for today — duplicate run detected.")
        print("Exiting without trading. (This is the safety guard working.)")
        sys.exit(0)

    # 0b. Risk guardrails — if halted, we don't even scan.
    account = executor.get_account()
    ok, reason = executor.guardrails_pass(account)
    trade_logger.log_equity(account)
    if not ok:
        print(f"HALTED, no trades today: {reason}")
        sys.exit(0)

    # 1. Scan
    print("\n[1/4] Scanning pre-market gappers...")
    candidates = scanner.build_candidates()
    print(f"      {len(candidates)} candidates with news catalysts")
    for c in candidates:
        print(f"      {c['symbol']:6s} +{c['gap_pct']}%  |  {c['news'][0]['headline'][:70]}")

    # 2. Analyze
    print("\n[2/4] Sending to Claude for catalyst analysis...")
    decision = analyst.analyze(candidates)
    trade_logger.log_decision(candidates, decision)
    print(f"      Market note: {decision.get('market_note', '')}")
    print(f"      Trades recommended: {len(decision['trades'])}")

    # 2b. Shadow risk gate — OBSERVES ONLY. Logs what it would veto/flag,
    #     never alters the decision. Firewalled: a gate crash can't stop trading.
    try:
        gate = shadow_gate.evaluate(
            candidates, decision,
            open_position_count=len(executor.get_open_positions()),
        )
        s = gate["summary"]
        print(f"      Shadow gate: {s['would_allow']} allow / {s['flagged']} flagged / {s['would_veto']} would-veto (advisory only)")
    except Exception as e:  # noqa: BLE001
        print(f"      Shadow gate error (ignored, live run unaffected): {e}")

    # 2c. Shadow challenger — second Claude call plays risk officer and
    #     confirms/rejects each proposal. Logged only; forms the v2 track.
    try:
        chall = challenger.review(candidates, decision)
        verdicts = {r["symbol"]: r["verdict"] for r in chall.get("reviews", [])}
        if verdicts:
            print("      Challenger:", ", ".join(f"{k}={v}" for k, v in verdicts.items()), "(advisory only)")
    except Exception as e:  # noqa: BLE001
        print(f"      Challenger error (ignored, live run unaffected): {e}")

    # 2d. Benchmark — log SPY close for the dashboard's market-comparison line.
    try:
        benchmark.log_spy()
    except Exception as e:  # noqa: BLE001
        print(f"      Benchmark log error (ignored): {e}")

    # 3. Execute
    print("\n[3/4] Placing bracket orders (PAPER)...")
    price_by_symbol = {c["symbol"]: c["last_price"] for c in candidates}
    for trade in decision["trades"]:
        sym = trade["symbol"]
        ref_price = price_by_symbol.get(sym)
        if ref_price is None:
            execution = {"skipped": True, "reason": "symbol not in candidate list (hallucination guard)"}
        else:
            try:
                execution = executor.place_bracket(sym, ref_price)
            except Exception as e:  # noqa: BLE001
                execution = {"skipped": True, "reason": f"order error: {e}"}
        trade_logger.log_trade(trade, execution)
        status = "SKIPPED: " + execution.get("reason", "") if execution.get("skipped") else \
            f"BUY {execution['qty']} @ ~{execution['ref_price']} | stop {execution['stop']} | target {execution['target']}"
        print(f"      {sym:6s} conviction {trade.get('conviction')}/10 -> {status}")

    # 4. Done
    print("\n[4/4] Run complete. Decisions logged to logs/.")
    print("      Brackets manage exits. TIF=day flattens by close.")


if __name__ == "__main__":
    main()
