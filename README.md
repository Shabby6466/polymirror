# PolyMirror — Polymarket Copy Trading Platform

A full-stack copy trading bot for Polymarket with MetaMask login,
Supabase persistence, paper/live modes, and real-time trade tracking.

---

## Stack

| Layer | Tech |
|---|---|
| Frontend | Vanilla HTML/CSS/JS (drop-in, no build step) |
| Backend | Python FastAPI + uvicorn |
| Database | Supabase (PostgreSQL + Row Level Security) |
| Auth | MetaMask sign-in (SIWE pattern) + JWT |
| Trading | py-clob-client (Polymarket official SDK) |
| Chain | Polygon (MATIC + USDC) |

---

## Project structure

```
polymirror/
├── index.html           # Frontend — serve from any static host
├── server.py            # FastAPI backend
├── supabase_schema.sql  # Run once in Supabase SQL editor
├── setup.py             # One-time USDC allowance + API key setup
├── .env                 # Secrets (never commit)
└── requirements.txt
```

---

## Setup steps

### 1. Supabase

1. Create a free project at https://supabase.com
2. Go to SQL Editor → paste and run `supabase_schema.sql`
3. Copy your project URL and service role key

### 2. .env file

```env
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_KEY=eyJh...your_service_role_key

# Your Polygon wallet for LIVE order execution
BOT_PRIVATE_KEY=0x...

# Optional — defaults work
CLOB_HOST=https://clob.polymarket.com
CHAIN_ID=137
JWT_SECRET=any_long_random_string_here
```

### 3. Install backend dependencies

```bash
pip install -r requirements.txt
```

### 4. Run one-time setup (LIVE mode only)

```bash
python setup.py
# Sets USDC allowances + derives CLOB API key
```

### 5. Start the backend

```bash
uvicorn server:app --reload --port 8000
```

### 6. Open the frontend

Open `index.html` in your browser directly, or serve it:

```bash
python -m http.server 3000
# Then visit http://localhost:3000
```

---

## How paper vs live mode works

| Feature | Paper | Live |
|---|---|---|
| Wallet tracking | ✓ Separate wallet list | ✓ Separate wallet list |
| Trade execution | Simulated instantly | Real on-chain via CLOB |
| P&L tracking | Based on price at copy | Based on actual fills |
| Settings | Independent | Independent |
| Data isolation | Fully separate in DB | Fully separate in DB |

Wallets added in paper mode are stored with `mode='paper'` in Supabase
and are completely invisible to the live bot — and vice versa.

---

## Safe wallet removal

When you try to remove a tracked wallet:

1. The backend checks for open trades from that wallet in the current mode
2. If none → wallet is removed immediately
3. If open trades exist → a confirmation dialog shows the list of open trades
4. If confirmed with `force=true` → open trades are marked closed, wallet removed

---

## Architecture notes

- **Bot loops** run as async tasks per user per mode (so each user's bots are independent)
- **WebSocket** pushes new trade events to the frontend in real-time
- **Price updater** background task refreshes P&L for all open trades every 60s
- **Daily rate limit** per tracked wallet resets at UTC midnight
- **Seen trade ID** cache is primed on startup from DB + live API to avoid re-copying history

---

## Requirements

```
fastapi
uvicorn[standard]
python-dotenv
supabase
py-clob-client
requests
web3
eth-account
python-jose[cryptography]
websockets
```
