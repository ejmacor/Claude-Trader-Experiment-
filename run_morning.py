"""
run_morning.py — The daily entry point. Run this at 8:30am ET each
trading day (manually or via scheduler).

Pipeline: scan -> Claude analyzes -> guardrails -> bracket orders -> log.
"""

import sys

import analyst
import executor
import scanner
import trade_logger


def main():
    print("=" * 60)
    print("CLAUDE TRADER — morning run")
    print("=" * 60)

    # 0. Guardrails first — if halted, we don't even scan.
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
