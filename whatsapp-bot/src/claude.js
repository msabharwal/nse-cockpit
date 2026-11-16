// Free-text questions → Claude, grounded in today's feed only.
import { dateLabel, normSymbol } from "./feed.js";

const SYSTEM = `You are the NSE Cockpit assistant, replying on WhatsApp.
You explain end-of-day technical data for Indian (NSE) stocks, using ONLY the DATA block you are given.

Rules — always follow them:
- Information and education only. Never tell the user to buy, sell, hold, enter or exit anything. Never give target prices, stop-losses, position sizes or price predictions. If asked for any of these, say you can only explain the data and suggest a SEBI-registered adviser for personal advice.
- Use only numbers in DATA. If something isn't there (news, results, fundamentals, intraday prices, F&O data), say you don't have it.
- Explain indicators in plain words when helpful (e.g. what RSI 72 usually means), without implying what will happen next.
- WhatsApp formatting: *bold* for emphasis, short lines, no markdown headers, no tables. Keep the reply under 900 characters.

DATA columns per stock: s=symbol, p=close price ₹, c=% change vs previous close (null if unknown), r=RSI(14), v=volume ÷ 20-day average, g=% gap from 10-day SMA, sl=100-day SMA slope (%/day), a=ATR as % of price, w=position in 52-week range %.`;

function mentionedSymbols(text, idx, limit = 8) {
  const words = String(text).toUpperCase().replace(/[^A-Z0-9&\- ]/g, " ").split(/\s+/).filter(Boolean);
  const found = new Set();
  // single words and joined pairs/triples ("TATA MOTORS" → TATAMOTORS)
  for (let i = 0; i < words.length; i++) {
    for (let n = 1; n <= 3 && i + n <= words.length; n++) {
      const cand = normSymbol(words.slice(i, i + n).join(""));
      if (cand.length >= 2 && idx.has(cand)) found.add(cand);
    }
  }
  return [...found].slice(0, limit);
}

export async function askClaude(env, text, { feed, idx, watch }) {
  if (!env.ANTHROPIC_API_KEY) return null;

  const syms = new Set([...mentionedSymbols(text, idx), ...watch.slice(0, 15)]);
  const rows = [...syms].map((s) => idx.get(s)).filter(Boolean);
  const data = {
    date: dateLabel(feed.d),
    market: feed.digest,
    stocks_count: feed.n,
    cols: feed.cols,
    stocks: rows,
    user_watchlist: watch,
  };

  const res = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "x-api-key": env.ANTHROPIC_API_KEY,
      "anthropic-version": "2023-06-01",
      "content-type": "application/json",
    },
    body: JSON.stringify({
      model: env.CLAUDE_MODEL || "claude-haiku-5-5",
      max_tokens: 600,
      system: SYSTEM,
      messages: [{ role: "user", content: `DATA:\n${JSON.stringify(data)}\n\nUser message:\n${text}` }],
    }),
  });
  if (!res.ok) {
    console.error("Claude API error", res.status, await res.text());
    return null;
  }
  const out = await res.json();
  return (out.content || []).filter((b) => b.type === "text").map((b) => b.text).join("").trim() || null;
}
