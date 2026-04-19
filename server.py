"""
Polymarket Copy Trader — Backend API
======================================
FastAPI server handling:
  - MetaMask authentication (sign-in-with-ethereum / nonce challenge)
  - Supabase persistence (wallets, settings, trades, stats)
  - Bot engine (per-user polling, paper + live execution)
  - WebSocket push for real-time trade feed

Requirements:
    pip install fastapi uvicorn python-dotenv supabase \
                py-clob-client requests web3 eth-account \
                python-jose[cryptography] websockets

Run:
    uvicorn server:app --reload --port 8000
"""

import os
import json
import time
import base64
import asyncio
import logging
import secrets
import hashlib
from datetime import datetime, timedelta
from typing import Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from jose import jwt, JWTError
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3
from cryptography.fernet import Fernet, InvalidToken
from supabase import create_client, Client

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY as BUY_SIDE, SELL as SELL_SIDE

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# Config
# ─────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
JWT_SECRET = os.getenv("JWT_SECRET", secrets.token_hex(32))
JWT_ALGO = "HS256"
JWT_EXPIRE_HOURS = 24 * 7  # 1 week sessions

CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
DATA_API = "https://data-api.polymarket.com"
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
app = FastAPI(title="Polymarket Copy Trader API")

# ─────────────────────────────────────────
# Private-key encryption (Fernet, key = sha256(JWT_SECRET))
# ─────────────────────────────────────────
_FERNET = Fernet(base64.urlsafe_b64encode(hashlib.sha256(JWT_SECRET.encode()).digest()))

def encrypt_secret(plain: str) -> str:
    return _FERNET.encrypt(plain.encode()).decode()

def decrypt_secret(cipher: str) -> str:
    return _FERNET.decrypt(cipher.encode()).decode()

# Polygon RPC + USDC.e (the contract Polymarket uses)
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")
USDC_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}],
     "type": "function"},
]
_w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
_usdc = _w3.eth.contract(address=USDC_ADDRESS, abi=ERC20_ABI)

def read_balances(eoa: str) -> dict:
    """Returns {matic, usdc} for an EOA on Polygon."""
    addr = Web3.to_checksum_address(eoa)
    try:
        matic_wei = _w3.eth.get_balance(addr)
    except Exception:
        matic_wei = 0
    try:
        usdc_raw = _usdc.functions.balanceOf(addr).call()
    except Exception:
        usdc_raw = 0
    return {
        "matic": round(matic_wei / 1e18, 6),
        "usdc": round(usdc_raw / 1e6, 4),
        "address": addr,
    }

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restrict to your frontend domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer()

# ─────────────────────────────────────────
# Nonce store (in-memory; use Redis in prod)
# ─────────────────────────────────────────
nonce_store: dict[str, str] = {}  # wallet_address -> nonce


# ─────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────

def create_jwt(user_id: str, wallet_address: str) -> str:
    payload = {
        "sub": user_id,
        "wallet": wallet_address.lower(),
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    return decode_jwt(credentials.credentials)


# ─────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────

class NonceRequest(BaseModel):
    wallet_address: str

class VerifyRequest(BaseModel):
    wallet_address: str
    signature: str

class AddWalletRequest(BaseModel):
    mode: str          # 'paper' | 'live'
    wallet_address: str
    nickname: str

class RemoveWalletRequest(BaseModel):
    mode: str
    tracked_wallet_id: str
    force: bool = False  # if True, close open trades before removing

class UpdateSettingsRequest(BaseModel):
    mode: str
    copy_amount_usdc: float
    min_original_trade_size: float
    max_trades_per_wallet_per_day: int
    copy_sells: bool
    poll_interval_seconds: int

class UpdatePrivateKeyRequest(BaseModel):
    private_key: str   # stored encrypted; only used server-side for signing

import re
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")

def _normalize_pk(pk: str) -> str:
    """Strip whitespace, drop leading 0x, validate it's 64 hex chars, return 0x-prefixed."""
    pk = (pk or "").strip()
    if pk.lower().startswith("0x"):
        pk = pk[2:]
    if not _HEX64.match(pk):
        raise HTTPException(
            status_code=400,
            detail="Invalid private key format. Expected 64 hexadecimal characters (with or without 0x prefix).",
        )
    # Reject the all-zero key (mathematically invalid for secp256k1)
    if int(pk, 16) == 0:
        raise HTTPException(status_code=400, detail="Private key cannot be zero.")
    return "0x" + pk.lower()

def get_user_private_key(user_id: str) -> str | None:
    """Returns the decrypted private key for a user, or None if none stored."""
    row = supabase.table("users").select("encrypted_private_key")\
        .eq("id", user_id).single().execute().data
    enc = row.get("encrypted_private_key") if row else None
    if not enc:
        return None
    try:
        return decrypt_secret(enc)
    except InvalidToken:
        log.error(f"Failed to decrypt key for user {user_id}")
        return None


# ─────────────────────────────────────────
# Auth endpoints
# ─────────────────────────────────────────

@app.post("/auth/nonce")
async def get_nonce(req: NonceRequest):
    """Generate a random nonce for the wallet to sign."""
    nonce = secrets.token_hex(16)
    nonce_store[req.wallet_address.lower()] = nonce
    return {"nonce": nonce, "message": f"Sign this message to log in to Polymarket Copy Trader.\n\nNonce: {nonce}"}


@app.post("/auth/verify")
async def verify_signature(req: VerifyRequest):
    """
    Verify the MetaMask signature against the stored nonce.
    Returns a JWT on success.
    """
    addr = req.wallet_address.lower()
    nonce = nonce_store.get(addr)
    if not nonce:
        raise HTTPException(status_code=400, detail="No nonce found. Request a nonce first.")

    message = f"Sign this message to log in to Polymarket Copy Trader.\n\nNonce: {nonce}"
    try:
        w3 = Web3()
        msg = encode_defunct(text=message)
        recovered = w3.eth.account.recover_message(msg, signature=req.signature).lower()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Signature verification failed: {e}")

    if recovered != addr:
        raise HTTPException(status_code=401, detail="Signature does not match wallet address")

    # Clear the nonce (one-time use)
    del nonce_store[addr]

    # Upsert user in Supabase
    result = supabase.table("users").upsert(
        {"wallet_address": addr, "last_seen_at": datetime.utcnow().isoformat()},
        on_conflict="wallet_address"
    ).execute()
    user = result.data[0]

    # Seed default settings if new user
    for mode in ("paper", "live"):
        supabase.table("copy_settings").upsert({
            "user_id": user["id"],
            "mode": mode,
            "copy_amount_usdc": 20,
            "min_original_trade_size": 10,
            "max_trades_per_wallet_per_day": 5,
            "copy_sells": True,
            "poll_interval_seconds": 30,
        }, on_conflict="user_id,mode").execute()

    token = create_jwt(user["id"], addr)
    return {"token": token, "user_id": user["id"], "wallet_address": addr}


# ─────────────────────────────────────────
# Wallet management
# ─────────────────────────────────────────

@app.get("/wallets/{mode}")
async def get_wallets(mode: str, user=Depends(get_current_user)):
    result = supabase.table("tracked_wallets")\
        .select("*")\
        .eq("user_id", user["sub"])\
        .eq("mode", mode)\
        .execute()
    return result.data


@app.post("/wallets/add")
async def add_wallet(req: AddWalletRequest, user=Depends(get_current_user)):
    if req.mode not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'")

    # Validate it looks like an Ethereum address
    if not req.wallet_address.startswith("0x") or len(req.wallet_address) != 42:
        raise HTTPException(status_code=400, detail="Invalid wallet address format")

    result = supabase.table("tracked_wallets").insert({
        "user_id": user["sub"],
        "mode": req.mode,
        "wallet_address": req.wallet_address.lower(),
        "nickname": req.nickname,
        "enabled": True,
    }).execute()
    return result.data[0]


@app.post("/wallets/remove")
async def remove_wallet(req: RemoveWalletRequest, user=Depends(get_current_user)):
    """
    Removes a tracked wallet.
    If open trades exist and force=False, returns a warning with trade count.
    If force=True, marks open trades as closed first.
    """
    # Check for open trades from this wallet
    open_trades = supabase.table("copied_trades")\
        .select("id, market_question, usdc_spent")\
        .eq("user_id", user["sub"])\
        .eq("mode", req.mode)\
        .eq("tracked_wallet_id", req.tracked_wallet_id)\
        .eq("status", "open")\
        .execute().data

    if open_trades and not req.force:
        return {
            "requires_confirmation": True,
            "open_trade_count": len(open_trades),
            "open_trades": open_trades,
            "message": f"This wallet has {len(open_trades)} open trade(s). Set force=true to remove anyway.",
        }

    if open_trades and req.force:
        # Close all open trades (mark as closed with current P&L)
        for trade in open_trades:
            supabase.table("copied_trades").update({
                "status": "closed",
                "closed_at": datetime.utcnow().isoformat(),
            }).eq("id", trade["id"]).execute()

    supabase.table("tracked_wallets")\
        .delete()\
        .eq("id", req.tracked_wallet_id)\
        .eq("user_id", user["sub"])\
        .execute()

    return {"success": True, "closed_trades": len(open_trades) if open_trades else 0}


@app.patch("/wallets/{wallet_id}/toggle")
async def toggle_wallet(wallet_id: str, user=Depends(get_current_user)):
    row = supabase.table("tracked_wallets")\
        .select("enabled")\
        .eq("id", wallet_id)\
        .eq("user_id", user["sub"])\
        .single().execute().data
    new_state = not row["enabled"]
    supabase.table("tracked_wallets")\
        .update({"enabled": new_state})\
        .eq("id", wallet_id).execute()
    return {"enabled": new_state}


# ─────────────────────────────────────────
# Settings
# ─────────────────────────────────────────

@app.get("/settings/{mode}")
async def get_settings(mode: str, user=Depends(get_current_user)):
    result = supabase.table("copy_settings")\
        .select("*")\
        .eq("user_id", user["sub"])\
        .eq("mode", mode)\
        .single().execute()
    return result.data


@app.put("/settings")
async def update_settings(req: UpdateSettingsRequest, user=Depends(get_current_user)):
    supabase.table("copy_settings").update({
        "copy_amount_usdc": req.copy_amount_usdc,
        "min_original_trade_size": req.min_original_trade_size,
        "max_trades_per_wallet_per_day": req.max_trades_per_wallet_per_day,
        "copy_sells": req.copy_sells,
        "poll_interval_seconds": req.poll_interval_seconds,
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("user_id", user["sub"]).eq("mode", req.mode).execute()
    return {"success": True}


# ─────────────────────────────────────────
# Per-user live trading credentials (private key + balance)
# ─────────────────────────────────────────

@app.put("/user/private-key")
async def set_private_key(req: UpdatePrivateKeyRequest, user=Depends(get_current_user)):
    # 1. Format check (raises 400 with specific reason)
    pk = _normalize_pk(req.private_key)

    # 2. Cryptographic validity — eth_account derives the EOA from the key
    try:
        acct = Account.from_key(pk)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Key is not a valid secp256k1 private key: {e}")

    # 3. End-to-end usability check — ask Polymarket's CLOB to derive API
    #    creds using a signature from this key. If the CLOB accepts it, the
    #    key can sign trade requests for real.
    try:
        client = ClobClient(host=CLOB_HOST, key=pk, chain_id=CHAIN_ID)
        creds = client.derive_api_key()
        if not creds or not getattr(creds, "api_key", None):
            raise RuntimeError("CLOB returned empty credentials")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Polymarket rejected this key — it can't sign orders. Details: {e}",
        )

    supabase.table("users").update({
        "encrypted_private_key": encrypt_secret(pk),
    }).eq("id", user["sub"]).execute()

    return {"success": True, "eoa_address": acct.address}


@app.delete("/user/private-key")
async def delete_private_key(user=Depends(get_current_user)):
    supabase.table("users").update({"encrypted_private_key": None})\
        .eq("id", user["sub"]).execute()
    # If a live bot is running, stop it
    key = f"{user['sub']}:live"
    task = running_bots.get(key)
    if task and not task.done():
        task.cancel()
        del running_bots[key]
    return {"success": True}


@app.get("/user/private-key/status")
async def private_key_status(user=Depends(get_current_user)):
    pk = get_user_private_key(user["sub"])
    if not pk:
        return {"has_key": False}
    try:
        return {"has_key": True, "eoa_address": Account.from_key(pk).address}
    except Exception:
        return {"has_key": False}


@app.post("/user/approve-contracts")
async def approve_contracts(user=Depends(get_current_user)):
    """One-time USDC/CTF allowance approval for Polymarket contracts.
    Requires MATIC in the trading wallet for gas."""
    pk = get_user_private_key(user["sub"])
    if not pk:
        raise HTTPException(status_code=400, detail="No private key configured")
    try:
        client = ClobClient(host=CLOB_HOST, key=pk, chain_id=CHAIN_ID)
        creds = client.derive_api_key()
        client.set_api_creds(creds)
        try:
            client.set_allowances()
        except Exception as e:
            # Often "already set" — not always fatal
            log.warning(f"set_allowances: {e}")
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Approval failed: {e}")


@app.get("/user/balance")
async def get_balance(user=Depends(get_current_user)):
    pk = get_user_private_key(user["sub"])
    if not pk:
        raise HTTPException(status_code=400, detail="No private key configured")
    try:
        eoa = Account.from_key(pk).address
    except Exception:
        raise HTTPException(status_code=400, detail="Stored key is invalid")
    return read_balances(eoa)


# ─────────────────────────────────────────
# Stats & trades
# ─────────────────────────────────────────

@app.get("/stats/{mode}")
async def get_stats(mode: str, user=Depends(get_current_user)):
    try:
        result = supabase.rpc("get_user_stats", {
            "p_user_id": user["sub"],
            "p_mode": mode,
        }).execute()
    except Exception:
        pass # Ignore failure, we will use the fallback logic below

    # Fallback: compute from view if RPC not set up
    trades = supabase.table("copied_trades")\
        .select("*")\
        .eq("user_id", user["sub"])\
        .eq("mode", mode)\
        .execute().data

    total_invested = sum(t.get("usdc_spent") or 0 for t in trades)
    total_pnl = sum(t.get("pnl") or 0 for t in trades)
    open_trades = [t for t in trades if t["status"] == "open"]
    closed_trades = [t for t in trades if t["status"] != "open"]
    winning = [t for t in closed_trades if (t.get("pnl") or 0) > 0]

    return {
        "total_trades": len(trades),
        "open_trades": len(open_trades),
        "closed_trades": len(closed_trades),
        "total_invested": round(total_invested, 2),
        "total_pnl": round(total_pnl, 2),
        "roi_pct": round((total_pnl / total_invested * 100) if total_invested > 0 else 0, 2),
        "win_rate": round((len(winning) / len(closed_trades) * 100) if closed_trades else 0, 1),
        "winning_trades": len(winning),
    }


@app.get("/trades/{mode}")
async def get_trades(mode: str, limit: int = 50, user=Depends(get_current_user)):
    result = supabase.table("copied_trades")\
        .select("*, tracked_wallets(nickname)")\
        .eq("user_id", user["sub"])\
        .eq("mode", mode)\
        .order("opened_at", desc=True)\
        .limit(limit)\
        .execute()
    return result.data


@app.get("/snapshots/{mode}")
async def get_snapshots(mode: str, user=Depends(get_current_user)):
    result = supabase.table("portfolio_snapshots")\
        .select("*")\
        .eq("user_id", user["sub"])\
        .eq("mode", mode)\
        .order("snapshot_at", desc=True)\
        .limit(30)\
        .execute()
    return result.data


@app.get("/suggested-wallets")
async def suggested_wallets(window: str = "1d", metric: str = "profit",
                            limit: int = 10, user=Depends(get_current_user)):
    """Top-performing Polymarket wallets from the public leaderboard.
    metric: 'profit' | 'volume'; window: '1d' | '1w' | '1m' | 'all'"""
    if metric not in ("profit", "volume"):
        raise HTTPException(status_code=400, detail="metric must be 'profit' or 'volume'")
    if window not in ("1d", "7d", "30d", "all"):
        raise HTTPException(status_code=400, detail="window must be 1d|7d|30d|all")
    try:
        r = requests.get(f"https://lb-api.polymarket.com/{metric}",
                         params={"window": window, "limit": limit},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
        rows = r.json() or []
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Leaderboard fetch failed: {e}")

    # Flag which are already tracked by this user, per mode
    tracked = supabase.table("tracked_wallets").select("wallet_address, mode")\
        .eq("user_id", user["sub"]).execute().data or []
    tracked_by_mode = {"paper": set(), "live": set()}
    for t in tracked:
        tracked_by_mode.setdefault(t["mode"], set()).add(t["wallet_address"].lower())

    out = []
    for row in rows:
        addr = (row.get("proxyWallet") or "").lower()
        if not addr:
            continue
        name = row.get("pseudonym") or row.get("name") or addr[:10]
        # Strip auto-generated pseudonyms (the address echoed as name)
        if name.lower().startswith("0x"):
            name = f"Anon {addr[:6]}"
        out.append({
            "address": addr,
            "name": name,
            "amount": row.get("amount"),
            "profile_image": row.get("profileImageOptimized") or row.get("profileImage") or "",
            "already_tracked": {
                "paper": addr in tracked_by_mode["paper"],
                "live": addr in tracked_by_mode["live"],
            },
        })
    return {"window": window, "metric": metric, "wallets": out}


@app.get("/history/{mode}")
async def get_history(mode: str, user=Depends(get_current_user)):
    wallets = supabase.table("tracked_wallets")\
        .select("wallet_address, nickname")\
        .eq("user_id", user["sub"])\
        .eq("mode", mode)\
        .eq("enabled", True)\
        .execute().data
        
    all_trades = []
    for w in wallets:
        try:
            trades = fetch_recent_trades(w["wallet_address"], limit=20)
            for t in trades:
                trade_type = str(t.get("type", "")).upper()
                if trade_type in ("BUY", "SELL"):
                    t["_nickname"] = w["nickname"]
                    t["_wallet"] = w["wallet_address"]
                    all_trades.append(t)
        except Exception as e:
            print(f"Failed to fetch history for {w['wallet_address']}: {e}")
            
    # Attempt to sort by timestamp if available
    def get_ts(trade):
        ts = trade.get("timestamp") or trade.get("createdAt") or 0
        try:
            return int(ts)
        except:
            return 0
    all_trades.sort(key=get_ts, reverse=True)
    return all_trades[:50]


# ─────────────────────────────────────────
# Bot control
# ─────────────────────────────────────────

# Track running bot tasks per user+mode
running_bots: dict[str, asyncio.Task] = {}  # key: "user_id:mode"


@app.post("/bot/{mode}/start")
async def start_bot(mode: str, user=Depends(get_current_user)):
    if mode not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'")
    if mode == "live" and not get_user_private_key(user["sub"]):
        raise HTTPException(
            status_code=400,
            detail="No private key configured. Add your trading key in the Live Wallet panel first.",
        )
    key = f"{user['sub']}:{mode}"
    if key in running_bots and not running_bots[key].done():
        return {"status": "already_running"}
    task = asyncio.create_task(bot_loop(user["sub"], mode))
    running_bots[key] = task
    return {"status": "started"}


@app.post("/bot/{mode}/stop")
async def stop_bot(mode: str, user=Depends(get_current_user)):
    key = f"{user['sub']}:{mode}"
    task = running_bots.get(key)
    if task and not task.done():
        task.cancel()
        del running_bots[key]
        return {"status": "stopped"}
    return {"status": "not_running"}


@app.get("/bot/{mode}/status")
async def bot_status(mode: str, user=Depends(get_current_user)):
    key = f"{user['sub']}:{mode}"
    task = running_bots.get(key)
    running = task is not None and not task.done()
    return {"running": running}


# ─────────────────────────────────────────
# WebSocket — real-time trade feed
# ─────────────────────────────────────────

ws_connections: dict[str, list[WebSocket]] = {}  # user_id -> [sockets]


@app.websocket("/ws/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str):
    try:
        payload = decode_jwt(token)
        user_id = payload["sub"]
    except HTTPException:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    ws_connections.setdefault(user_id, []).append(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep alive
    except WebSocketDisconnect:
        ws_connections[user_id].remove(websocket)


async def push_trade_event(user_id: str, event: dict):
    """Push a new trade event to all connected WebSocket clients for this user."""
    for ws in ws_connections.get(user_id, []):
        try:
            await ws.send_json(event)
        except Exception:
            pass


# ─────────────────────────────────────────
# Polymarket API helpers
# ─────────────────────────────────────────

def fetch_recent_trades(wallet_address: str, limit: int = 50) -> list[dict]:
    """
    Returns normalized trades for a proxy wallet from the Polymarket data API.
    Normalizes Polymarket's field names to what the bot loop expects:
      side → type, size*price → usdcSize, title → question,
      transactionHash → id, asset → tokenId
    """
    try:
        resp = requests.get(
            f"{DATA_API}/trades",
            params={"user": wallet_address.lower(), "limit": limit},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()
        raw = raw if isinstance(raw, list) else raw.get("data", [])
        out = []
        for t in raw:
            try:
                size = float(t.get("size") or 0)
                price = float(t.get("price") or 0)
            except (TypeError, ValueError):
                size, price = 0, 0
            out.append({
                "id": t.get("transactionHash") or t.get("id"),
                "type": str(t.get("side", "")).upper(),
                "usdcSize": round(size * price, 6),
                "size": size,
                "price": price,
                "question": t.get("title") or t.get("question") or "",
                "conditionId": t.get("conditionId") or t.get("market") or "",
                "tokenId": str(t.get("asset") or t.get("tokenId") or ""),
                "outcome": t.get("outcome", ""),
                "outcomeIndex": t.get("outcomeIndex", 0),
                "timestamp": t.get("timestamp"),
                "transactionHash": t.get("transactionHash"),
                "proxyWallet": t.get("proxyWallet"),
            })
        return out
    except Exception as e:
        log.warning(f"Fetch trades failed for {wallet_address}: {e}")
        return []


def get_market_info(condition_id: str) -> dict | None:
    try:
        resp = requests.get(
            f"{DATA_API}/markets/{condition_id}",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"},
            timeout=10
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def get_best_price(client: ClobClient, token_id: str, side: str) -> float | None:
    try:
        book = client.get_order_book(token_id)
        if side == "BUY" and book.asks:
            return float(book.asks[0].price)
        elif side == "SELL" and book.bids:
            return float(book.bids[0].price)
    except Exception:
        pass
    return None


# ─────────────────────────────────────────
# Bot loop (async, per user+mode)
# ─────────────────────────────────────────

async def bot_loop(user_id: str, mode: str):
    """
    Main async polling loop for one user in one mode.
    Runs until cancelled.
    """
    log.info(f"Bot started: user={user_id} mode={mode}")

    def load_settings() -> dict:
        try:
            return supabase.table("copy_settings").select("*")\
                .eq("user_id", user_id).eq("mode", mode).single().execute().data or {}
        except Exception as e:
            log.warning(f"copy_settings missing for user={user_id} mode={mode}: {e}")
            return {}

    DEFAULTS = {
        "copy_amount_usdc": 20,
        "min_original_trade_size": 0,
        "max_trades_per_wallet_per_day": 0,  # 0 = unlimited (copy all actions)
        "copy_sells": True,
        "poll_interval_seconds": 30,
    }
    settings = {**DEFAULTS, **load_settings()}

    # Init CLOB client (only needed for live mode) — per-user key from DB
    clob_client = None
    if mode == "live":
        user_pk = get_user_private_key(user_id)
        if not user_pk:
            log.error(f"[LIVE] No private key stored for user {user_id} — aborting bot")
            return
        try:
            clob_client = ClobClient(host=CLOB_HOST, key=user_pk, chain_id=CHAIN_ID)
            try:
                creds = clob_client.derive_api_key()
                clob_client.set_api_creds(creds)
            except Exception as e:
                log.warning(f"[LIVE] derive_api_key failed (may already be set): {e}")
        except Exception as e:
            log.error(f"[LIVE] CLOB client init failed: {e}")
            return

    # Seen trade IDs — prime from DB so we never re-copy on restart
    existing = supabase.table("copied_trades")\
        .select("source_trade_id, source_wallet_address")\
        .eq("user_id", user_id)\
        .eq("mode", mode)\
        .execute().data
    seen_ids: set[str] = {r["source_trade_id"] for r in existing}
    wallets_with_history: set[str] = {r["source_wallet_address"] for r in existing}

    BACKFILL_N = 3   # how many recent trades to copy when first observing a new wallet
    known_wallets: set[str] = set()
    # NOTE: we don't pre-prime seen_ids from the live API here — backfill is
    # handled per-wallet inside the loop (see "first observation" block) so that
    # newly-added wallets immediately mirror their latest few actions.

    daily_counts: dict[str, dict] = {}  # address -> {date, count}

    while True:
        try:
            # Re-fetch wallets each cycle (user may have added/removed)
            wallets = supabase.table("tracked_wallets")\
                .select("*")\
                .eq("user_id", user_id)\
                .eq("mode", mode)\
                .eq("enabled", True)\
                .execute().data

            # Re-fetch settings each cycle (user may have changed them)
            settings = {**DEFAULTS, **load_settings()}

            for wallet in wallets:
                address = wallet["wallet_address"]
                nickname = wallet["nickname"]
                wallet_id = wallet["id"]

                raw_trades = fetch_recent_trades(address)

                # First time we observe this wallet in this bot session.
                # PAPER:  backfill the most recent N trades so user sees activity.
                # LIVE:   never backfill — only forward-copy fresh trades, so we
                #         don't spend real USDC on stale whale signals.
                if address not in known_wallets:
                    known_wallets.add(address)
                    if mode == "paper" and address not in wallets_with_history:
                        sorted_trades = sorted(
                            raw_trades, key=lambda x: int(x.get("timestamp") or 0), reverse=True)
                        skip = sorted_trades[BACKFILL_N:]
                        for t in skip:
                            tid = str(t.get("id") or "")
                            if tid:
                                seen_ids.add(tid)
                        log.info(f"[{nickname}] paper backfill: up to {BACKFILL_N} recent trades")
                    else:
                        # Live, or wallet already has history — skip everything currently visible
                        for t in raw_trades:
                            tid = str(t.get("id") or "")
                            if tid:
                                seen_ids.add(tid)
                        if mode == "live":
                            log.info(f"[{nickname}] live mode: forward-copy only (no backfill)")

                for trade in raw_trades:
                    trade_id = str(trade.get("id") or trade.get("tradeId") or "")
                    if not trade_id or trade_id in seen_ids:
                        continue
                    seen_ids.add(trade_id)

                    trade_type = str(trade.get("type", "")).upper()
                    if trade_type not in ("BUY", "SELL"):
                        continue
                    if trade_type == "SELL" and not settings["copy_sells"]:
                        continue

                    try:
                        original_size = float(trade.get("usdcSize") or trade.get("amount") or 0)
                    except (TypeError, ValueError):
                        original_size = 0
                    
                    # Disabled 'Min original size' per user request - always copying trades regardless of size
                    # if original_size < settings["min_original_trade_size"]:
                    #     continue

                    # Daily rate limit — 0 or missing means unlimited (copy all actions)
                    daily_cap = int(settings.get("max_trades_per_wallet_per_day") or 0)
                    if daily_cap > 0:
                        today = str(datetime.utcnow().date())
                        entry = daily_counts.get(address, {})
                        if entry.get("date") != today:
                            daily_counts[address] = {"date": today, "count": 0}
                        if daily_counts[address]["count"] >= daily_cap:
                            log.info(f"[{nickname}] Daily limit reached, skipping")
                            continue

                    # Execute copy
                    trade_record = await execute_trade(
                        user_id=user_id,
                        mode=mode,
                        wallet_id=wallet_id,
                        wallet_address=address,
                        source_trade_id=trade_id,
                        trade=trade,
                        settings=settings,
                        clob_client=clob_client,
                    )

                    if trade_record:
                        if daily_cap > 0:
                            daily_counts[address]["count"] += 1
                        await push_trade_event(user_id, {
                            "type": "new_trade",
                            "mode": mode,
                            "trade": trade_record,
                        })

        except asyncio.CancelledError:
            log.info(f"Bot cancelled: user={user_id} mode={mode}")
            return
        except Exception as e:
            log.error(f"Bot loop error (user={user_id} mode={mode}): {e}", exc_info=True)

        await asyncio.sleep(settings.get("poll_interval_seconds", 30))


async def execute_trade(
    user_id: str,
    mode: str,
    wallet_id: str,
    wallet_address: str,
    source_trade_id: str,
    trade: dict,
    settings: dict,
    clob_client: ClobClient | None,
) -> dict | None:
    """
    Executes or simulates a copied trade and saves it to Supabase.
    """
    condition_id = trade.get("conditionId") or trade.get("market") or ""
    trade_type = str(trade.get("type", "")).upper()
    outcome = trade.get("outcome", "")
    market_question = trade.get("question") or trade.get("title") or condition_id
    token_id = str(trade.get("tokenId") or trade.get("asset") or "")
    # Copy the whale's exact USDC size. Fall back to the user's configured
    # per-trade amount only if the whale's size is missing/zero.
    try:
        whale_usdc = float(trade.get("usdcSize") or 0)
    except (TypeError, ValueError):
        whale_usdc = 0
    try:
        fallback_amount = float(settings.get("copy_amount_usdc") or 20)
    except (TypeError, ValueError):
        fallback_amount = 20.0
    copy_amount = whale_usdc if whale_usdc > 0 else fallback_amount

    # Resolve token_id from market info if needed
    if not token_id and condition_id:
        market = get_market_info(condition_id)
        if market:
            tokens = market.get("tokens", [])
            idx = int(trade.get("outcomeIndex", 0))
            if idx < len(tokens):
                token_id = tokens[idx].get("token_id", "")

    price_at_copy = None
    shares = None
    order_id = None
    tx_hash = None
    success = False

    if mode == "paper":
        # Paper mode: prefer live CLOB price for realism, fall back to whale's price
        price_at_copy = None
        if token_id:
            try:
                r = requests.get(f"{CLOB_HOST}/price",
                                 params={"token_id": token_id,
                                         "side": "buy" if trade_type == "BUY" else "sell"},
                                 timeout=4)
                if r.ok:
                    price_at_copy = float(r.json().get("price") or 0) or None
            except Exception:
                pass
        if not price_at_copy:
            try:
                price_at_copy = float(trade.get("price") or 0.5)
            except (TypeError, ValueError):
                price_at_copy = 0.5
        shares = round(copy_amount / price_at_copy, 4) if price_at_copy > 0 else 0
        success = True
        log.info(f"[PAPER] {trade_type} {shares} @ ${price_at_copy:.3f} spend=${copy_amount:.2f} | {market_question[:50]}")

    elif mode == "live" and clob_client:
        side = BUY_SIDE if trade_type == "BUY" else SELL_SIDE
        price_at_copy = get_best_price(clob_client, token_id, trade_type)
        if not price_at_copy or price_at_copy <= 0 or price_at_copy >= 1:
            log.warning(f"[LIVE] Invalid price {price_at_copy} — skipping")
            return None
        shares = round(copy_amount / price_at_copy, 4)
        try:
            order_args = OrderArgs(token_id=token_id, price=price_at_copy, size=shares, side=side)
            # GTC: rests on the book if not immediately fillable (rather than FOK-reject)
            resp = clob_client.create_and_post_order(order_args, OrderType.GTC)
            if resp and (resp.get("orderID") or resp.get("success")):
                order_id = resp.get("orderID")
                tx_hash = resp.get("transactionHash")
                success = True
                log.info(f"[LIVE] {trade_type} {shares} @ {price_at_copy} → {order_id}")
            else:
                log.error(f"[LIVE] Order rejected: {resp}")
                return None
        except Exception as e:
            log.error(f"[LIVE] Order failed: {e}")
            return None

    if not success:
        return None

    # SELL: close the matching open BUY position (FIFO per source wallet + token)
    if trade_type == "SELL":
        matches = supabase.table("copied_trades")\
            .select("id, shares, usdc_spent, price_at_copy")\
            .eq("user_id", user_id).eq("mode", mode)\
            .eq("source_wallet_address", wallet_address)\
            .eq("token_id", token_id).eq("side", "BUY").eq("status", "open")\
            .order("opened_at").execute().data or []
        remaining = shares or 0
        realized = 0.0
        for m in matches:
            if remaining <= 0:
                break
            m_shares = float(m.get("shares") or 0)
            m_cost = float(m.get("usdc_spent") or 0)
            take = min(m_shares, remaining)
            cost_basis = m_cost * (take / m_shares) if m_shares else 0
            proceeds = take * price_at_copy
            realized += (proceeds - cost_basis)
            new_shares = m_shares - take
            update = {"pnl": round((float(m.get("pnl") or 0)) + (proceeds - cost_basis), 4)}
            if new_shares <= 1e-9:
                update.update({"status": "closed", "closed_at": datetime.utcnow().isoformat(),
                               "shares": 0, "current_price": price_at_copy})
            else:
                update.update({"shares": round(new_shares, 6),
                               "usdc_spent": round(m_cost - cost_basis, 4),
                               "current_price": price_at_copy})
            supabase.table("copied_trades").update(update).eq("id", m["id"]).execute()
            remaining -= take
        if realized:
            log.info(f"[{mode.upper()}] SELL closed matching positions, realized P&L ${realized:+.2f}")

    # Persist to Supabase
    record = {
        "user_id": user_id,
        "mode": mode,
        "tracked_wallet_id": wallet_id,
        "source_wallet_address": wallet_address,
        "source_trade_id": source_trade_id,
        "condition_id": condition_id,
        "market_question": market_question[:500],
        "outcome": outcome,
        "side": trade_type,
        "token_id": token_id,
        "price_at_copy": price_at_copy,
        "shares": shares,
        "usdc_spent": copy_amount,
        "status": "open",
        "current_price": price_at_copy,
        "pnl": 0,
        "order_id": order_id,
        "tx_hash": tx_hash,
    }
    result = supabase.table("copied_trades").insert(record).execute()
    return result.data[0] if result.data else None


# ─────────────────────────────────────────
# Price updater (background task)
# ─────────────────────────────────────────

@app.on_event("startup")
async def startup_tasks():
    asyncio.create_task(price_updater())


async def price_updater():
    """
    Periodically updates current_price and pnl for all open trades.
    Runs every 60 seconds.
    """
    while True:
        await asyncio.sleep(60)
        try:
            open_trades = supabase.table("copied_trades")\
                .select("id, token_id, shares, usdc_spent, price_at_copy")\
                .eq("status", "open")\
                .execute().data

            for trade in open_trades:
                if not trade.get("token_id"):
                    continue
                # Fetch latest price from Polymarket data API
                try:
                    resp = requests.get(
                        f"{CLOB_HOST}/price",
                        params={"token_id": trade["token_id"], "side": "buy"},
                        timeout=5,
                    )
                    if resp.ok:
                        price_data = resp.json()
                        current_price = float(price_data.get("price") or price_data.get("midpoint") or trade["price_at_copy"])
                        shares = float(trade.get("shares") or 0)
                        usdc_spent = float(trade.get("usdc_spent") or 0)
                        current_value = shares * current_price
                        pnl = current_value - usdc_spent
                        supabase.table("copied_trades").update({
                            "current_price": current_price,
                            "pnl": round(pnl, 4),
                        }).eq("id", trade["id"]).execute()
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"Price updater error: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
