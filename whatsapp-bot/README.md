# NSE Cockpit — WhatsApp (via Twilio)

End-of-day NSE data on WhatsApp, in two stages:

| | **Stage 1 — one-way** | **Stage 2 — two-way bot** |
|---|---|---|
| What people get | Evening update after market close | Evening update **plus** symbol lookups, watchlists, alerts, questions answered by Claude |
| Runs on | GitHub Actions only (`bot/send_whatsapp.py`) | Cloudflare Worker + D1 (this folder), triggered by GitHub Actions |
| Watchlists | Set by you in a GitHub secret | Each user manages their own by chat |
| Setup | ~15 minutes | ~45 minutes more |

```
GitHub Actions (7 PM IST, Mon–Fri)
  nse_pipeline.py ─► output/signals.json
  bot/build_feed.py ─► output/bot.json (compact; adds % change vs the previous history snapshot)
  git push
  ├─ Stage 1: bot/send_whatsapp.py ─► Twilio ─► WhatsApp      (only while WHATSAPP_BOT_URL is unset)
  └─ Stage 2: POST <worker>/broadcast ─► Worker ─► Twilio ─► WhatsApp
                                         ▲
             people's replies ─► Twilio ─┘ (webhook)
```

The service is deliberately **information-only**: no buy/sell/hold calls, targets or stop-losses.
SEBI treats those as research recommendations that need RA/IA registration, even on WhatsApp.
Claude is instructed the same way. Keep the recipient list / `ALLOWED_NUMBERS` to people you
know until the compliance route for a public launch is settled.

---

## The 24-hour rule (applies to Twilio and Meta alike)

WhatsApp delivers free-form messages only to people who messaged your number in the last
24 hours. To reach anyone else you must use an **approved template**. So:

- **Testing with the Twilio sandbox:** each tester sends the `join <code>` message, then you can
  send free-form for 24 hours (sandbox membership itself lasts 3 days, then they re-join).
  Leave `TWILIO_CONTENT_SID` empty — they get the full update.
- **Production:** create the template below and set `TWILIO_CONTENT_SID`. People who haven't
  written in the last 24 hours get a one-line summary via the template. In Stage 2 they can
  reply `TODAY` for the full update, and anyone who chatted recently gets the full update directly.

### The template (create once)
Twilio Console → **Messaging → Content Template Builder → Create new**
- Name: `nse_daily_update` · Type: **Text** · Language: English · Category: **Utility**
- Body:
  ```
  NSE Cockpit · {{1}} close

  {{2}}

  Information only, not investment advice.
  ```
- Sample values: `{{1}}` = `14 Nov 2026`, `{{2}}` = `Breadth 1310 up / 1246 down · Gainers: KMEW +3.00% · Watchlist: TCS ₹2,436 (+1.19%)`
- Submit for WhatsApp approval, then copy its **Content SID** (`HX…`).

Meta decides the category; if it re-classifies the template as Marketing, it still works but costs more per message.

---

## Stage 1 — one-way from GitHub Actions

1. **Twilio:** use your sandbox number to test (Console → Messaging → Try it out → Send a WhatsApp
   message), or your approved WhatsApp sender for production.
2. **GitHub** → repo → Settings → Secrets and variables → Actions → add:

   | Secret | Example |
   |---|---|
   | `TWILIO_ACCOUNT_SID` | `AC…` |
   | `TWILIO_AUTH_TOKEN` | from the Twilio Console home page |
   | `TWILIO_WHATSAPP_FROM` | `+14155238886` (sandbox) or your sender number |
   | `WHATSAPP_RECIPIENTS` | `+919812345678=TCS,INFY,RELIANCE; +919811111111=HDFCBANK` |
   | `TWILIO_CONTENT_SID` | optional — `HX…` once the template is approved |

   Each recipient can have its own watchlist after `=`; separate people with `;`.
3. That's it: the next pipeline run sends the update. To test now, run the workflow manually
   (Actions → NSE Daily Pipeline → Run workflow), or locally:
   ```bash
   WHATSAPP_RECIPIENTS="+919812345678=TCS" python bot/send_whatsapp.py --dry-run   # prints, sends nothing
   ```

---

## Stage 2 — two-way bot (Cloudflare Worker)

### What users can send
| Message | Reply |
|---|---|
| `hi` / `help` | command list |
| `today` | market update + their watchlist + any triggered alerts |
| `RELIANCE` (any NSE symbol) | price, % change, RSI, volume vs average, trend, ATR, 52-week position |
| `watch TCS INFY` / `unwatch TCS` / `list` | watchlist |
| `alert TCS above 4200`, `alert INFY rsi below 30` | one-shot alerts, checked on each close |
| `clear alerts`, `stop`, `start` | housekeeping |
| anything else | answered by Claude using only that day's data |

### Setup
```bash
cd whatsapp-bot
npm install
npx wrangler login                     # free Cloudflare account
npx wrangler d1 create nse-bot         # copy the database_id into wrangler.toml
npm run db:init                        # creates the tables
npx wrangler secret put TWILIO_ACCOUNT_SID
npx wrangler secret put TWILIO_AUTH_TOKEN
npx wrangler secret put TWILIO_WHATSAPP_FROM
npx wrangler secret put TWILIO_CONTENT_SID     # optional, once approved
npx wrangler secret put BROADCAST_SECRET       # any long random string
npx wrangler secret put ANTHROPIC_API_KEY      # optional: enables free-text questions
```
Put the allowed numbers in `ALLOWED_NUMBERS` in `wrangler.toml` (digits with country code,
comma-separated, e.g. `"919812345678,919811111111"`), then:
```bash
npx wrangler deploy        # prints https://nse-cockpit-bot.<you>.workers.dev
```

**Point Twilio at the bot:** Twilio Console → Messaging → (sandbox settings, or your WhatsApp
sender) → **"When a message comes in"** = `https://nse-cockpit-bot.<you>.workers.dev/webhook`, method **POST**.
Use exactly this URL — Twilio signs requests with it and the bot rejects anything that doesn't match.

**Switch GitHub over to the bot:** add secrets `WHATSAPP_BOT_URL` (the worker URL, no trailing slash)
and `BROADCAST_SECRET` (same value as above). Stage 1 then switches itself off automatically.
Users add their own watchlists by chat; `WHATSAPP_RECIPIENTS` is no longer used.

Test: send `hi` to your Twilio WhatsApp number. To push today's update right away:
```bash
curl -X POST "$WHATSAPP_BOT_URL/broadcast?force=1" -H "Authorization: Bearer $BROADCAST_SECRET" -d '{}'
```

---

## Costs
- **Twilio:** a small per-message fee on top of WhatsApp's own charges. Replies inside the 24-hour
  window carry no WhatsApp charge; template messages do. Check current India rates in the Twilio Console.
- **Cloudflare Workers + D1:** free tier to start. The free plan allows ~50 outbound calls per request,
  so one broadcast reaches roughly 40 people; Workers Paid ($5/month) lifts that to ~1,000.
- **Claude:** only for free-text questions; `claude-haiku-5-5` by default (`CLAUDE_MODEL`).

## Using Meta directly instead of Twilio
Set `WA_PROVIDER = "meta"` and `WA_TEMPLATE_NAME` in `wrangler.toml`, set the `WA_TOKEN`,
`WA_PHONE_NUMBER_ID`, `WA_VERIFY_TOKEN` and `WA_APP_SECRET` secrets, and point Meta's webhook at
`/webhook` (subscribe to `messages`). Same template body, with `{{1}}`/`{{2}}`.

## Local tests (no accounts needed)
```bash
cd whatsapp-bot
npm test                         # simulated conversations + broadcasts via Twilio
PROVIDER=meta npm test           # same via Meta
python ../bot/send_whatsapp.py --dry-run   # Stage 1 (needs WHATSAPP_RECIPIENTS)
```

## Data notes
- Only technical fields are shown. P/E, ROE, D/E and sector in `signals.json` are still placeholder
  defaults for most stocks, so the messages leave them out until real fundamentals are wired in.
- % change and breadth need a history snapshot from the previous trading day; they appear from the
  second daily run onward.
- Long messages are split on line breaks into parts of up to 1,600 characters (Twilio's WhatsApp limit).
