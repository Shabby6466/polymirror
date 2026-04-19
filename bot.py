# """
# Polymarket Copy Trading Bot
# ============================
# Polls tracked wallets for new trades and mirrors them on-chain
# using the official py-clob-client SDK.

# Requirements:
#     pip install py-clob-client python-dotenv requests

# .env file:
#     PRIVATE_KEY=0x...
#     RPC_URL=https://polygon-rpc.com
#     CLOB_HOST=https://clob.polymarket.com
#     CHAIN_ID=137

# wallets.json: see README / setup guide
# """

# import os
# import json
# import time
# import logging
# import requests
# from datetime import datetime, date
# from dotenv import load_dotenv

# from py_clob_client.client import ClobClient
# from py_clob_client.clob_types import OrderArgs, OrderType, BUY, SELL
# from py_clob_client.order_builder.constants import BUY as BUY_SIDE, SELL as SELL_SIDE

# # ─────────────────────────────────────────
# # Logging setup
# # ─────────────────────────────────────────
# logging.basicConfig(
#     level=logging.INFO,
#     format="%(asctime)s [%(levelname)s] %(message)s",
#     handlers=[
#         logging.StreamHandler(),
#         logging.FileHandler("bot.log"),
#     ],
# )
# log = logging.getLogger(__name__)


# # ─────────────────────────────────────────
# # Config loader
# # ─────────────────────────────────────────

# def load_config(path: str = "wallets.json") -> dict:
#     with open(path, "r") as f:
#         config = json.load(f)
#     required_settings = [
#         "copy_amount_usdc",
#         "min_original_trade_size",
#         "max_trades_per_wallet_per_day",
#         "poll_interval_seconds",
#     ]
#     for key in required_settings:
#         if key not in config.get("settings", {}):
#             raise ValueError(f"Missing setting '{key}' in wallets.json")
#     return config


# # ─────────────────────────────────────────
# # Polymarket Data API helpers
# # ─────────────────────────────────────────

# DATA_API = "https://data-api.polymarket.com"

# def fetch_recent_trades(wallet_address: str, limit: int = 50) -> list[dict]:
#     """
#     Fetches recent trade activity for a given wallet address
#     from the Polymarket data API.
#     """
#     url = f"{DATA_API}/activity"
#     params = {
#         "user": wallet_address.lower(),
#         "limit": limit,
#     }
#     try:
#         resp = requests.get(url, params=params, timeout=10)
#         resp.raise_for_status()
#         data = resp.json()
#         # The endpoint returns a list directly or wrapped in a dict
#         if isinstance(data, list):
#             return data
#         return data.get("data", [])
#     except requests.RequestException as e:
#         log.warning(f"Failed to fetch trades for {wallet_address}: {e}")
#         return []


# def get_market_info(condition_id: str) -> dict | None:
#     """
#     Fetches market metadata (token IDs, question, etc.)
#     for a given condition ID.
#     """
#     url = f"{DATA_API}/markets/{condition_id}"
#     try:
#         resp = requests.get(url, timeout=10)
#         resp.raise_for_status()
#         return resp.json()
#     except requests.RequestException as e:
#         log.warning(f"Failed to fetch market info for {condition_id}: {e}")
#         return None


# def get_best_price(client: ClobClient, token_id: str, side: str) -> float | None:
#     """
#     Fetches the current best available price for a token
#     from the CLOB order book.
#     Side: 'BUY' or 'SELL'
#     """
#     try:
#         book = client.get_order_book(token_id)
#         if side == "BUY" and book.asks:
#             # Best ask = lowest price you can buy at
#             return float(book.asks[0].price)
#         elif side == "SELL" and book.bids:
#             # Best bid = highest price you can sell at
#             return float(book.bids[0].price)
#     except Exception as e:
#         log.warning(f"Could not fetch order book for {token_id}: {e}")
#     return None


# # ─────────────────────────────────────────
# # Trade detection
# # ─────────────────────────────────────────

# def extract_new_trades(
#     wallet: dict,
#     seen_ids: set,
#     settings: dict,
# ) -> list[dict]:
#     """
#     Polls a wallet's activity, filters to trades we haven't
#     seen yet, and returns only those that meet our copy criteria.
#     """
#     address = wallet["address"]
#     raw_trades = fetch_recent_trades(address)
#     new_trades = []

#     for trade in raw_trades:
#         trade_id = trade.get("id") or trade.get("tradeId")
#         if not trade_id:
#             continue

#         # Skip already-seen trades
#         if trade_id in seen_ids:
#             continue

#         # Mark as seen immediately to avoid double-processing
#         seen_ids.add(trade_id)

#         # Only process buys/sells, skip transfers or other event types
#         trade_type = str(trade.get("type", "")).upper()
#         if trade_type not in ("BUY", "SELL"):
#             continue

#         # Skip sells if the user opted out of copying exits
#         if trade_type == "SELL" and not settings.get("copy_sells", True):
#             continue

#         # Filter by minimum original trade size
#         try:
#             original_size = float(trade.get("usdcSize") or trade.get("amount") or 0)
#         except (TypeError, ValueError):
#             original_size = 0

#         if original_size < settings["min_original_trade_size"]:
#             log.debug(
#                 f"Skipping small trade ${original_size:.2f} "
#                 f"(min: ${settings['min_original_trade_size']})"
#             )
#             continue

#         new_trades.append(trade)

#     return new_trades


# # ─────────────────────────────────────────
# # Rate limiting
# # ─────────────────────────────────────────

# class DailyTradeTracker:
#     """
#     Tracks how many trades have been copied per wallet per day
#     and enforces the max_trades_per_wallet_per_day limit.
#     """

#     def __init__(self):
#         self._counts: dict[str, dict] = {}  # address -> {date: str, count: int}

#     def can_trade(self, address: str, max_per_day: int) -> bool:
#         today = str(date.today())
#         entry = self._counts.get(address)
#         if not entry or entry["date"] != today:
#             self._counts[address] = {"date": today, "count": 0}
#             return True
#         return entry["count"] < max_per_day

#     def record_trade(self, address: str):
#         today = str(date.today())
#         entry = self._counts.get(address)
#         if not entry or entry["date"] != today:
#             self._counts[address] = {"date": today, "count": 1}
#         else:
#             entry["count"] += 1

#     def count_today(self, address: str) -> int:
#         today = str(date.today())
#         entry = self._counts.get(address)
#         if not entry or entry["date"] != today:
#             return 0
#         return entry["count"]


# # ─────────────────────────────────────────
# # Order execution
# # ─────────────────────────────────────────

# def execute_copy_trade(
#     client: ClobClient,
#     trade: dict,
#     copy_amount_usdc: float,
#     wallet_nickname: str,
# ) -> bool:
#     """
#     Places a copy of the detected trade using the CLOB client.
#     Returns True on success, False on failure.
#     """
#     # Extract the outcome token ID and side from the detected trade
#     token_id = trade.get("outcomeIndex") or trade.get("tokenId") or trade.get("asset")
#     condition_id = trade.get("conditionId") or trade.get("market")
#     trade_type = str(trade.get("type", "")).upper()
#     outcome = trade.get("outcome", "?")

#     if not token_id and condition_id:
#         # Try to resolve token_id from market info
#         market = get_market_info(condition_id)
#         if market:
#             tokens = market.get("tokens", [])
#             outcome_index = int(trade.get("outcomeIndex", 0))
#             if outcome_index < len(tokens):
#                 token_id = tokens[outcome_index].get("token_id")

#     if not token_id:
#         log.warning(f"Could not resolve token_id for trade {trade.get('id')} — skipping")
#         return False

#     # Determine CLOB side
#     side = BUY_SIDE if trade_type == "BUY" else SELL_SIDE

#     # Fetch current best price
#     price = get_best_price(client, str(token_id), trade_type)
#     if price is None or price <= 0 or price >= 1:
#         log.warning(f"Invalid price {price} for token {token_id} — skipping")
#         return False

#     # Calculate share count from our fixed USDC amount
#     shares = round(copy_amount_usdc / price, 2)

#     question = trade.get("question") or trade.get("title") or condition_id or "Unknown market"
#     log.info(
#         f"  Copying: [{wallet_nickname}] {trade_type} {shares} shares of "
#         f"'{outcome}' @ ${price:.4f} | Market: {question[:60]}"
#     )

#     try:
#         order_args = OrderArgs(
#             token_id=str(token_id),
#             price=price,
#             size=shares,
#             side=side,
#         )
#         # Place a market-style limit order (FOK = Fill or Kill, immediate)
#         resp = client.create_and_post_order(order_args, OrderType.FOK)
#         if resp and resp.get("orderID"):
#             log.info(f"  ✓ Order placed: {resp['orderID']}")
#             return True
#         else:
#             log.warning(f"  Order response unexpected: {resp}")
#             return False
#     except Exception as e:
#         log.error(f"  ✗ Order failed: {e}")
#         return False


# # ─────────────────────────────────────────
# # Main bot loop
# # ─────────────────────────────────────────

# def run_bot():
#     load_dotenv()

#     private_key = os.getenv("PRIVATE_KEY")
#     rpc_url = os.getenv("RPC_URL", "https://polygon-rpc.com")
#     clob_host = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
#     chain_id = int(os.getenv("CHAIN_ID", "137"))

#     if not private_key:
#         raise EnvironmentError("PRIVATE_KEY not set in .env")

#     config = load_config("wallets.json")
#     settings = config["settings"]
#     wallets = [w for w in config["wallets"] if w.get("enabled", True)]

#     if not wallets:
#         log.warning("No enabled wallets in wallets.json — nothing to copy.")
#         return

#     log.info(f"Starting Polymarket Copy Bot")
#     log.info(f"  Tracking {len(wallets)} wallet(s)")
#     log.info(f"  Copy amount: ${settings['copy_amount_usdc']} USDC per trade")
#     log.info(f"  Poll interval: {settings['poll_interval_seconds']}s")

#     # Initialize CLOB client
#     client = ClobClient(
#         host=clob_host,
#         key=private_key,
#         chain_id=chain_id,
#     )

#     # One-time API key derivation (creates/caches credentials)
#     try:
#         client.derive_api_key()
#         log.info("  CLOB API key ready")
#     except Exception as e:
#         log.warning(f"  API key derivation skipped or failed: {e}")

#     # State: seen trade IDs and daily trade counter
#     seen_ids: set[str] = set()
#     daily_tracker = DailyTradeTracker()

#     # Prime the seen set on startup — don't copy historical trades
#     log.info("Priming seen trade IDs (skipping historical trades)...")
#     for wallet in wallets:
#         initial = fetch_recent_trades(wallet["address"])
#         for t in initial:
#             tid = t.get("id") or t.get("tradeId")
#             if tid:
#                 seen_ids.add(tid)
#     log.info(f"  Loaded {len(seen_ids)} historical trade IDs to ignore")

#     total_copied = 0
#     total_failed = 0

#     log.info("Bot is live. Watching for new trades...\n")

#     while True:
#         try:
#             for wallet in wallets:
#                 address = wallet["address"]
#                 nickname = wallet.get("nickname", address[:10])

#                 new_trades = extract_new_trades(wallet, seen_ids, settings)

#                 if not new_trades:
#                     continue

#                 log.info(f"[{nickname}] {len(new_trades)} new trade(s) detected")

#                 for trade in new_trades:
#                     # Check daily rate limit
#                     max_per_day = settings["max_trades_per_wallet_per_day"]
#                     if not daily_tracker.can_trade(address, max_per_day):
#                         today_count = daily_tracker.count_today(address)
#                         log.info(
#                             f"  [{nickname}] Daily limit reached "
#                             f"({today_count}/{max_per_day}) — skipping"
#                         )
#                         continue

#                     success = execute_copy_trade(
#                         client=client,
#                         trade=trade,
#                         copy_amount_usdc=settings["copy_amount_usdc"],
#                         wallet_nickname=nickname,
#                     )

#                     if success:
#                         daily_tracker.record_trade(address)
#                         total_copied += 1
#                     else:
#                         total_failed += 1

#             log.debug(
#                 f"Cycle complete — total copied: {total_copied}, "
#                 f"failed: {total_failed} | "
#                 f"sleeping {settings['poll_interval_seconds']}s"
#             )

#         except KeyboardInterrupt:
#             log.info("\nBot stopped by user.")
#             log.info(f"Session summary: {total_copied} copied, {total_failed} failed")
#             break
#         except Exception as e:
#             log.error(f"Unexpected error in main loop: {e}", exc_info=True)
#             log.info("Continuing after error...")

#         time.sleep(settings["poll_interval_seconds"])


# # ─────────────────────────────────────────
# # Entry point
# # ─────────────────────────────────────────

# if __name__ == "__main__":
#     run_bot()
