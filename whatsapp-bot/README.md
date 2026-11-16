# NSE Cockpit — WhatsApp bot

End-of-day NSE data on WhatsApp: an evening market update, per-stock lookups,
watchlists, price/RSI alerts, and plain-language Q&A (Claude).

```
GitHub Actions (7 PM IST, Mon–Fri)
  nse_pipeline.py ─► output/signals.json
  bot/build_feed.py ─► output/bot.json  (compact, ~150 KB, adds % change)
  git push ─► POST /broadcast {ref: <commit sha>}
                         │
Cloudflare Worker (this folder) ◄──── WhatsApp Cloud API webhook (user messages)
  D1 database: users, watchlists, alerts
  reads bot.json from GitHub at that commit
  replies via WhatsApp; free-text questions go to Claude
```

## What users can send

| Message | Reply |
|---|---|
| `hi` / `help` | command list |
| `today` | market update + their watchlist + any triggered alerts |
| `RELIANCE` (any NSE symbol) | price, % change, RSI, volume vs average, trend, ATR, 52-week position |
| `watch TCS INFY` / `unwatch TCS` / `list` | watchlist |
| `alert TCS above 4200`, `alert INFY rsi below 30` | one-shot alerts, checked on each close |
| `clear alerts`, `stop`, `start` | housekeeping |
| anything else | answered by Claude using only that day's data |

The bot is deliberately **information-only**: no buy/sell/hold calls, targets or
stop-losses (SEBI treats those as research recommendations that need RA/IA
registration). Claude is instructed the same way. Keep `ALLOWED_NUMBERS` set
(private beta) until the compliance route for a public launch is settled.

## Setup (about 1 hour, mostly Meta's screens)

### 1. Cloudflare (free)
```bash
cd whatsapp-bot
npm install
npx wrangler login
npx wrangler d1 create nse-bot        # copy the database_id into wrangler.toml
npm run db:init                       # creates the tables
npx wrangler deploy                   # prints your URL, e.g. https://nse-cockpit-bot.<you>.workers.dev
```

### 2. Meta / WhatsApp
1. Go to developers.facebook.com → **Create app** → type **Business** → add the **WhatsApp** product.
2. **WhatsApp → API Setup** gives you a free **test number**, a **Phone number ID** and a temporary token.
   The test number can message up to 5 verified recipients — perfect for the beta.
   Add your own number as a recipient.
3. For a token that doesn't expire: Business Settings → **System users** → add one → generate a token
   with `whatsapp_business_messaging` and `whatsapp_business_management`.
4. **App settings → Basic** → copy the **App secret**.
5. **WhatsApp → Configuration → Webhook**:
   - Callback URL: `https://<your worker>/webhook`
   - Verify token: any random string (you'll store the same value in step 3)
   - Subscribe to the **messages** field.
6. **WhatsApp Manager → Message templates → Create**:
   - Name `daily_market_update`, category **Utility**, language **English**
   - Body: `Your NSE Cockpit update for {{1}} is ready. Reply TODAY to see it.`
   - Sample for {{1}}: `16 Nov 2026`

   This template is only used for people who haven't messaged the bot in the last 24 hours
   (a WhatsApp rule). Everyone else gets the full update directly.
7. Later, for a real public number: add your own phone number and complete **Business verification**.

### 3. Worker secrets
```bash
npx wrangler secret put WA_TOKEN            # system-user token
npx wrangler secret put WA_PHONE_NUMBER_ID
npx wrangler secret put WA_VERIFY_TOKEN     # same string as in Meta's webhook settings
npx wrangler secret put WA_APP_SECRET
npx wrangler secret put BROADCAST_SECRET    # any long random string
npx wrangler secret put ANTHROPIC_API_KEY   # optional: enables free-text questions
```
Put your number(s) in `ALLOWED_NUMBERS` in `wrangler.toml` (e.g. `"919812345678"`) and `npx wrangler deploy` again.

### 4. GitHub
Repo → Settings → Secrets and variables → Actions → add:
- `WHATSAPP_BOT_URL` = your worker URL (no trailing slash)
- `BROADCAST_SECRET` = same value as above

The next pipeline run sends the evening update automatically. To test immediately:
```bash
curl -X POST "$WHATSAPP_BOT_URL/broadcast?force=1" -H "Authorization: Bearer $BROADCAST_SECRET" -d '{}'
```

### 5. Try it
Send `hi` to the WhatsApp number.

## Costs
- **Cloudflare Workers + D1**: free tier is enough to start. The free plan allows ~50 outbound calls per
  request, so one broadcast reaches roughly 40 subscribers; Workers Paid ($5/month) lifts that to ~1,000.
- **WhatsApp**: replies inside the 24h window are free; template messages are charged per message by Meta
  (check current India rates in WhatsApp Manager).
- **Claude**: only for free-text questions; `claude-haiku-5-5` by default (change `CLAUDE_MODEL`).

## Local test (no accounts needed)
```bash
npm test          # simulates conversations + broadcast against output/bot.json
```

## Data notes
- Only technical fields are shown. P/E, ROE, D/E and sector in `signals.json` are still placeholder
  defaults for most stocks, so the bot leaves them out until real fundamentals are wired in.
- % change and breadth appear from the second pipeline run onward (they compare with the previous `bot.json`).
