import asyncio
from server import fetch_recent_trades
def test():
    trades = fetch_recent_trades("0x633512f5a6da9b8cb8bc8595bf3afccfa0776b76", limit=5)
    print("Trades found:", len(trades))
    for t in trades:
        print(t.get("title"), t.get("side"), t.get("amount"))
if __name__ == "__main__":
    test()
