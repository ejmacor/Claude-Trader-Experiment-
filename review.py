"""
review.py — Run after the close (or anytime) to see how the experiment
is going. Pulls closed-order history from Alpaca and computes stats.
"""

import os
from collections import defaultdict

import requests

import executor


def get_closed_orders(limit=500):
    resp = requests.get(
        f"{executor.BASE_URL}/v2/orders",
        headers=executor.HEADERS,
        params={"status": "closed", "limit": limit, "direction": "desc"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def main():
    account = executor.get_account()
    equity = float(account["equity"])
    print("=" * 60)
    print("CLAUDE TRADER — review")
    print("=" * 60)
    print(f"Equity:        ${equity:,.2f}")
    print(f"Cash:          ${float(account['cash']):,.2f}")

    # Rough round-trip P&L from filled orders
    orders = [o for o in get_closed_orders() if o.get("filled_at")]
    fills = defaultdict(list)
    for o in orders:
        fills[o["symbol"]].append(o)

    wins, losses, total_pnl = 0, 0, 0.0
    for sym, olist in fills.items():
        buys = sum(float(o["filled_avg_price"]) * float(o["filled_qty"]) for o in olist if o["side"] == "buy")
        sells = sum(float(o["filled_avg_price"]) * float(o["filled_qty"]) for o in olist if o["side"] == "sell")
        bought_qty = sum(float(o["filled_qty"]) for o in olist if o["side"] == "buy")
        sold_qty = sum(float(o["filled_qty"]) for o in olist if o["side"] == "sell")
        if bought_qty > 0 and abs(bought_qty - sold_qty) < 1e-6:  # closed round trip
            pnl = sells - buys
            total_pnl += pnl
            wins += pnl > 0
            losses += pnl <= 0

    trades = wins + losses
    print(f"\nClosed trades: {trades}")
    if trades:
        print(f"Win rate:      {wins / trades * 100:.1f}%  ({wins}W / {losses}L)")
        print(f"Realized P&L:  ${total_pnl:,.2f}")
    print("\nFull decision history: logs/decisions.jsonl")
    print("Trade log:             logs/trade_log.csv")


if __name__ == "__main__":
    main()
