# """
# setup.py — Run this ONCE before starting the bot.

# Does two things:
#   1. Approves USDC spending allowance for Polymarket's contracts
#   2. Derives and caches your CLOB API key

# Usage:
#     python setup.py
# """

# import os
# from dotenv import load_dotenv
# from py_clob_client.client import ClobClient

# load_dotenv()

# private_key = os.getenv("PRIVATE_KEY")
# clob_host = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
# chain_id = int(os.getenv("CHAIN_ID", "137"))

# if not private_key:
#     raise EnvironmentError("PRIVATE_KEY not found in .env — please create it first")

# print("Connecting to Polymarket CLOB...")
# client = ClobClient(
#     host=clob_host,
#     key=private_key,
#     chain_id=chain_id,
# )

# print("Setting USDC allowances (this sends a transaction on Polygon)...")
# try:
#     client.set_allowances()
#     print("  ✓ Allowances set")
# except Exception as e:
#     print(f"  ✗ Allowance error (may already be set): {e}")

# print("Deriving CLOB API key...")
# try:
#     api_creds = client.derive_api_key()
#     print(f"  ✓ API key ready: {str(api_creds)[:40]}...")
# except Exception as e:
#     print(f"  ✗ API key error: {e}")

# print("\nSetup complete. You can now run: python bot.py")
