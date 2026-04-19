"""
setup.py — Run once before starting the bot.

Steps:
  1. Encrypts your private key and stores it in Supabase
  2. Sets USDC allowances on Polymarket (live mode only)
  3. Derives and caches your CLOB API key

Usage:
    python setup.py --mode paper
    python setup.py --mode live
"""

import os, argparse, hashlib, secrets
from dotenv import load_dotenv
from supabase import create_client
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

load_dotenv()

def derive_key(address, server_secret):
    return hashlib.sha256(f"{address.lower()}:{server_secret}".encode()).digest()

def encrypt_pk(pk, address, server_secret):
    key = derive_key(address, server_secret)
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, pk.encode(), None)
    return (nonce + ct).hex()

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["paper","live"], default="paper")
    args = p.parse_args()

    url     = os.getenv("SUPABASE_URL")
    key     = os.getenv("SUPABASE_KEY")
    address = os.getenv("USER_ADDRESS","").lower()
    pk      = os.getenv("PRIVATE_KEY","")
    secret  = os.getenv("SERVER_SECRET","change-me")

    if not all([url, key, address]):
        print("ERROR: Set SUPABASE_URL, SUPABASE_KEY, USER_ADDRESS in .env")
        return

    db = create_client(url, key)
    res = db.table("users").upsert({"address": address}, on_conflict="address").execute()
    user_id = res.data[0]["id"]
    print(f"User: {address} ({user_id})")

    if pk:
        enc = encrypt_pk(pk, address, secret)
        db.table("bot_settings").upsert({
            "user_id": user_id, "mode": args.mode, "encrypted_pk": enc,
        }, on_conflict="user_id,mode").execute()
        print(f"Private key encrypted and stored for mode: {args.mode}")
    else:
        print("No PRIVATE_KEY in .env — skipping key storage")

    existing = (db.table("bot_settings").select("id")
                .eq("user_id", user_id).eq("mode", args.mode)
                .maybe_single().execute())
    if not existing.data:
        db.table("bot_settings").insert({
            "user_id": user_id, "mode": args.mode,
            "copy_amount": 20, "max_per_day": 5, "min_size": 10,
            "poll_interval": 15, "copy_sells": True, "fok_orders": True,
            "rpc_url": "https://polygon-rpc.com",
        }).execute()
        print("Default settings created")

    if args.mode == "live" and pk:
        try:
            from py_clob_client.client import ClobClient
            client = ClobClient(host="https://clob.polymarket.com", key=pk, chain_id=137)
            print("Setting USDC allowances on Polygon...")
            client.set_allowances()
            print("  Allowances set")
            client.derive_api_key()
            print("  CLOB API key derived")
        except Exception as e:
            print(f"  CLOB setup error: {e}")

    print(f"\nDone. Run: python bot.py --mode {args.mode}")

if __name__ == "__main__":
    main()
