-- D1 schema for the NSE Cockpit WhatsApp bot
-- Apply with: npx wrangler d1 execute nse-bot --remote --file=schema.sql

CREATE TABLE IF NOT EXISTS users (
  phone            TEXT PRIMARY KEY,          -- WhatsApp ID, digits only, e.g. 919812345678
  name             TEXT,
  subscribed       INTEGER NOT NULL DEFAULT 1, -- 1 = receives the evening update
  created_at       TEXT NOT NULL,
  last_inbound_at  TEXT,                       -- opens WhatsApp's 24h free-reply window
  pending          TEXT                        -- alert notices waiting to be shown
);

CREATE TABLE IF NOT EXISTS watchlist (
  phone   TEXT NOT NULL,
  symbol  TEXT NOT NULL,
  PRIMARY KEY (phone, symbol)
);

CREATE TABLE IF NOT EXISTS alerts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  phone       TEXT NOT NULL,
  symbol      TEXT NOT NULL,
  kind        TEXT NOT NULL,   -- 'above' | 'below' (price) | 'rsi_above' | 'rsi_below'
  value       REAL NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_phone ON alerts(phone);

-- Small key/value store (e.g. feed_ref = commit SHA of the latest bot.json)
CREATE TABLE IF NOT EXISTS meta (
  k  TEXT PRIMARY KEY,
  v  TEXT
);

-- Meta can deliver the same webhook more than once; remember message IDs
CREATE TABLE IF NOT EXISTS seen (
  id   TEXT PRIMARY KEY,
  at   TEXT NOT NULL
);
