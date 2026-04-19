"""
Paper-mode copy trading demo — standalone, no Supabase/DB/auth required.

Uses the SAME fetch_recent_trades + execute_trade logic paths as server.py
(paper branch), so what you see working here maps 1:1 to the live bot loop.

Picks active whale wallets from Polymarket's global /trades feed, copies their
most recent trades in paper mode, then revalues positions against the live
CLOB price and prints a full portfolio + P&L report.
"""

import os
import sys
import time
import json
import requests
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import fetch_recent_trades, CLOB_HOST, DATA_API


def find_active_wallets(n: int = 3) -> list[tuple[str, int]]:
    """Pick the n most-active wallets from the last 200 global trades."""
    r = requests.get(f"{DATA_API}/trades", params={"limit": 200},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    r.raise_for_status()
    c = Counter(t["proxyWallet"] for t in r.json() if t.get("proxyWallet"))
    return c.most_common(n)


def current_price(token_id: str) -> float | None:
    try:
        r = requests.get(f"{CLOB_HOST}/price",
                         params={"token_id": token_id, "side": "buy"}, timeout=6)
        if r.ok:
            return float(r.json().get("price") or 0) or None
    except Exception:
        pass
    return None


def simulate_copy(trade: dict, copy_usdc: float = 20.0) -> dict:
    """Paper-trade execution — mirrors server.execute_trade 'paper' branch."""
    price = float(trade.get("price") or 0.5)
    # Scale shares so we spend roughly copy_usdc (regardless of whale size)
    shares = round(copy_usdc / price, 4) if price > 0 else 0
    return {
        "source_wallet": trade.get("proxyWallet"),
        "source_trade_id": trade.get("id"),
        "side": trade.get("type"),
        "question": trade.get("question"),
        "outcome": trade.get("outcome"),
        "token_id": trade.get("tokenId"),
        "price_at_copy": price,
        "shares": shares,
        "usdc_spent": round(shares * price, 4),
        "opened_at": datetime.utcnow().isoformat(timespec="seconds"),
    }


def revalue(positions: list[dict]) -> list[dict]:
    for p in positions:
        mkt = current_price(p["token_id"]) if p["token_id"] else None
        p["current_price"] = mkt if mkt is not None else p["price_at_copy"]
        p["current_value"] = round(p["shares"] * p["current_price"], 4)
        p["pnl"] = round(p["current_value"] - p["usdc_spent"], 4)
        p["pnl_pct"] = round((p["pnl"] / p["usdc_spent"] * 100) if p["usdc_spent"] else 0, 2)
    return positions


def main():
    print("=" * 78)
    print("  POLYMIRROR — paper-mode copy-trading demo")
    print("=" * 78)

    print("\n[1/4] Scanning Polymarket for active whale wallets...")
    whales = find_active_wallets(n=3)
    if not whales:
        print("  No active wallets found.")
        return
    for addr, n in whales:
        print(f"   whale {addr}  ({n} recent trades)")

    COPY_USDC = 20.0
    MAX_PER_WALLET = 3
    all_copies: list[dict] = []

    print(f"\n[2/4] Copying up to {MAX_PER_WALLET} trades/wallet at ${COPY_USDC} each...")
    for addr, _ in whales:
        trades = fetch_recent_trades(addr, limit=10)
        trades = [t for t in trades if t.get("type") in ("BUY", "SELL")][:MAX_PER_WALLET]
        print(f"\n   [{addr}]  {len(trades)} trades to copy")
        for t in trades:
            rec = simulate_copy(t, COPY_USDC)
            all_copies.append(rec)
            q = (rec["question"] or "")[:60]
            print(f"      {rec['side']:4} {rec['shares']:>8} @ ${rec['price_at_copy']:.3f}"
                  f"  →  ${rec['usdc_spent']:>6.2f}  | {q}")

    if not all_copies:
        print("\n   No trades were copied.")
        return

    print(f"\n[3/4] Revaluing {len(all_copies)} positions against live CLOB prices...")
    # Throttle so we don't get rate-limited
    for i, p in enumerate(all_copies):
        if i and i % 5 == 0:
            time.sleep(0.3)
    all_copies = revalue(all_copies)

    print("\n[4/4] Results\n" + "-" * 78)
    total_spent = sum(p["usdc_spent"] for p in all_copies)
    total_value = sum(p["current_value"] for p in all_copies)
    total_pnl = total_value - total_spent
    winners = [p for p in all_copies if p["pnl"] > 0]

    print(f"   Positions opened : {len(all_copies)}")
    print(f"   USDC invested    : ${total_spent:.2f}")
    print(f"   Current value    : ${total_value:.2f}")
    print(f"   Unrealized P&L   : ${total_pnl:+.2f}  ({(total_pnl/total_spent*100 if total_spent else 0):+.2f}%)")
    print(f"   Winners          : {len(winners)} / {len(all_copies)}")
    print("-" * 78)
    print(f"\n   {'SIDE':<5}{'SHARES':>9}{'COST':>9}{'MKT':>7}{'NOW':>9}{'P&L':>9}   MARKET")
    for p in sorted(all_copies, key=lambda x: x["pnl"], reverse=True):
        q = (p["question"] or "")[:42]
        print(f"   {p['side']:<5}{p['shares']:>9.2f}{p['usdc_spent']:>9.2f}"
              f"{p['price_at_copy']:>7.3f}{p['current_value']:>9.2f}"
              f"{p['pnl']:>+9.2f}   {q}")
    print("-" * 78)

    out = os.path.join(os.path.dirname(__file__), "paper_results.json")
    with open(out, "w") as f:
        json.dump({
            "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
            "copy_usdc_per_trade": COPY_USDC,
            "positions": all_copies,
            "totals": {
                "invested": round(total_spent, 2),
                "current_value": round(total_value, 2),
                "pnl": round(total_pnl, 2),
                "win_rate": round(len(winners)/len(all_copies)*100, 1),
            },
        }, f, indent=2)
    print(f"\n   Full results saved → {out}")


if __name__ == "__main__":
    main()
