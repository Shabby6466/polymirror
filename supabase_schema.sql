-- ============================================================
-- Polymarket Copy Trader — Supabase Schema
-- Run this in your Supabase SQL editor
-- ============================================================

-- Users authenticated via MetaMask wallet address
CREATE TABLE IF NOT EXISTS users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  wallet_address TEXT UNIQUE NOT NULL,          -- MetaMask address (lowercase)
  encrypted_private_key TEXT,                   -- Fernet-encrypted trading key (live mode)
  created_at TIMESTAMPTZ DEFAULT NOW(),
  last_seen_at TIMESTAMPTZ DEFAULT NOW()
);

-- For existing installs: add the column if it doesn't exist
ALTER TABLE users ADD COLUMN IF NOT EXISTS encrypted_private_key TEXT;

-- Tracked wallets per user, per mode
CREATE TABLE IF NOT EXISTS tracked_wallets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
  wallet_address TEXT NOT NULL,
  nickname TEXT NOT NULL DEFAULT 'Unnamed',
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  added_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(user_id, mode, wallet_address)
);

-- Copy settings per user per mode
CREATE TABLE IF NOT EXISTS copy_settings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
  copy_amount_usdc NUMERIC NOT NULL DEFAULT 20,
  min_original_trade_size NUMERIC NOT NULL DEFAULT 10,
  max_trades_per_wallet_per_day INTEGER NOT NULL DEFAULT 5,
  copy_sells BOOLEAN NOT NULL DEFAULT TRUE,
  poll_interval_seconds INTEGER NOT NULL DEFAULT 30,
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(user_id, mode)
);

-- All copied trades (both paper and live)
CREATE TABLE IF NOT EXISTS copied_trades (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
  tracked_wallet_id UUID REFERENCES tracked_wallets(id) ON DELETE SET NULL,
  source_wallet_address TEXT NOT NULL,
  source_trade_id TEXT NOT NULL,              -- original trade ID from Polymarket API
  condition_id TEXT NOT NULL,
  market_question TEXT,
  outcome TEXT,                               -- 'YES' or 'NO'
  side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
  token_id TEXT,
  price_at_copy NUMERIC,
  shares NUMERIC,
  usdc_spent NUMERIC,
  -- Position tracking
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open', 'closed', 'resolved')),
  current_price NUMERIC,                      -- updated periodically
  resolved_price NUMERIC,                     -- final payout price (0 or 1)
  pnl NUMERIC DEFAULT 0,                      -- unrealized or realized P&L in USDC
  -- On-chain info (live mode only)
  order_id TEXT,
  tx_hash TEXT,
  -- Timestamps
  opened_at TIMESTAMPTZ DEFAULT NOW(),
  closed_at TIMESTAMPTZ,
  UNIQUE(user_id, mode, source_trade_id)
);

-- Portfolio snapshots for charting (updated hourly by the bot)
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
  snapshot_at TIMESTAMPTZ DEFAULT NOW(),
  total_invested NUMERIC DEFAULT 0,
  total_current_value NUMERIC DEFAULT 0,
  realized_pnl NUMERIC DEFAULT 0,
  unrealized_pnl NUMERIC DEFAULT 0,
  total_trades INTEGER DEFAULT 0,
  winning_trades INTEGER DEFAULT 0
);

-- Index for fast lookups
CREATE INDEX IF NOT EXISTS idx_tracked_wallets_user_mode ON tracked_wallets(user_id, mode);
CREATE INDEX IF NOT EXISTS idx_copied_trades_user_mode ON copied_trades(user_id, mode);
CREATE INDEX IF NOT EXISTS idx_copied_trades_status ON copied_trades(status);
CREATE INDEX IF NOT EXISTS idx_copied_trades_source ON copied_trades(source_trade_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_user_mode ON portfolio_snapshots(user_id, mode, snapshot_at DESC);

-- ── Row Level Security ──────────────────────────────────────
-- Users can only see and modify their own data.
-- We use wallet_address stored in JWT claims (set by your backend).

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE tracked_wallets ENABLE ROW LEVEL SECURITY;
ALTER TABLE copy_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE copied_trades ENABLE ROW LEVEL SECURITY;
ALTER TABLE portfolio_snapshots ENABLE ROW LEVEL SECURITY;

-- Policy: service_role (backend) can do everything
-- Anon/authenticated users can only touch their own rows

CREATE POLICY "Users: own row only"
  ON users FOR ALL
  USING (id = auth.uid()::UUID OR auth.role() = 'service_role');

CREATE POLICY "Tracked wallets: own rows"
  ON tracked_wallets FOR ALL
  USING (user_id = auth.uid()::UUID OR auth.role() = 'service_role');

CREATE POLICY "Copy settings: own rows"
  ON copy_settings FOR ALL
  USING (user_id = auth.uid()::UUID OR auth.role() = 'service_role');

CREATE POLICY "Copied trades: own rows"
  ON copied_trades FOR ALL
  USING (user_id = auth.uid()::UUID OR auth.role() = 'service_role');

CREATE POLICY "Portfolio snapshots: own rows"
  ON portfolio_snapshots FOR ALL
  USING (user_id = auth.uid()::UUID OR auth.role() = 'service_role');

-- ── Helper view: per-user per-mode stats ────────────────────
CREATE OR REPLACE VIEW user_stats AS
SELECT
  ct.user_id,
  ct.mode,
  COUNT(*) FILTER (WHERE ct.status IN ('open','closed','resolved')) AS total_trades,
  COUNT(*) FILTER (WHERE ct.status = 'open') AS open_trades,
  COALESCE(SUM(ct.usdc_spent), 0) AS total_invested,
  COALESCE(SUM(ct.pnl), 0) AS total_pnl,
  COALESCE(SUM(ct.pnl) / NULLIF(SUM(ct.usdc_spent), 0) * 100, 0) AS roi_pct,
  COUNT(*) FILTER (WHERE ct.pnl > 0 AND ct.status != 'open') AS winning_trades,
  COUNT(*) FILTER (WHERE ct.pnl <= 0 AND ct.status != 'open') AS losing_trades
FROM copied_trades ct
GROUP BY ct.user_id, ct.mode;
