// Loads output/bot.json from GitHub and formats WhatsApp messages from it.

const TTL_MS = 5 * 60 * 1000;
let cache = { ref: null, at: 0, feed: null, idx: null };

// The broadcast stores the commit SHA of the newest bot.json in D1, so every
// Worker instance reads that exact version (raw.githubusercontent caches
// branch URLs for a few minutes; SHA URLs never go stale).
export async function getFeed(env, { force = false } = {}) {
  const row = await env.DB.prepare("SELECT v FROM meta WHERE k = 'feed_ref'").first();
  const ref = row?.v || env.FEED_BRANCH || "main";
  if (!force && cache.feed && cache.ref === ref && Date.now() - cache.at < TTL_MS) return cache;

  const url = `https://raw.githubusercontent.com/${env.FEED_REPO}/${ref}/${env.FEED_PATH || "output/bot.json"}`;
  const res = await fetch(url, { cf: { cacheTtl: 300 } });
  if (!res.ok) throw new Error(`Feed fetch failed: ${res.status} ${url}`);
  const feed = await res.json();
  const idx = new Map(feed.rows.map((r) => [r[0], r]));
  cache = { ref, at: Date.now(), feed, idx };
  return cache;
}

// ── Symbol helpers ────────────────────────────────────────────────────────────
export function normSymbol(s) {
  return String(s || "").toUpperCase().replace(/\.(NS|NSE|BO)$/, "").replace(/\s+/g, "");
}

export function suggest(idx, q, n = 5) {
  q = normSymbol(q);
  if (q.length < 2) return [];
  const out = [];
  for (const s of idx.keys()) {
    if (s.startsWith(q)) out.push(s);
    if (out.length >= n) break;
  }
  return out;
}

// ── Formatting ───────────────────────────────────────────────────────────────
const inr = (x) =>
  x == null ? "–" : "₹" + Number(x).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const pct = (x) => (x == null ? "" : `${x > 0 ? "+" : ""}${x.toFixed(2)}%`);

export function dateLabel(d) {
  return new Date(d + "T00:00:00Z").toLocaleDateString("en-IN", {
    day: "numeric", month: "short", year: "numeric", timeZone: "UTC",
  });
}

export const FOOTER = "_Information only — not investment advice._";

function rowObj(r) {
  const [s, p, c, rsi, v, g, sl, a, w] = r;
  return { s, p, c, rsi, v, g, sl, a, w };
}

export function stockLine(r) {
  const o = rowObj(r);
  const chg = o.c != null ? ` (${pct(o.c)})` : "";
  const rsi = o.rsi != null ? ` · RSI ${Math.round(o.rsi)}` : "";
  return `• ${o.s} ${inr(o.p)}${chg}${rsi}`;
}

export function stockCard(r, feed) {
  const o = rowObj(r);
  const L = [`*${o.s}*  ${inr(o.p)}${o.c != null ? `  (${pct(o.c)})` : ""}`, ""];
  if (o.rsi != null) {
    const zone = o.rsi >= 70 ? "overbought zone" : o.rsi <= 30 ? "oversold zone" : "neutral range";
    L.push(`RSI (14): ${o.rsi.toFixed(1)} — ${zone}`);
  }
  if (o.v != null) {
    const note = o.v >= 2 ? " — unusually heavy" : o.v <= 0.5 ? " — light" : "";
    L.push(`Volume: ${o.v.toFixed(2)}× its 20-day average${note}`);
  }
  if (o.g != null) L.push(`Price vs 10-day average: ${pct(o.g)}`);
  if (o.sl != null) {
    const t = o.sl > 0.02 ? "rising" : o.sl < -0.02 ? "falling" : "flat";
    L.push(`100-day average: ${t} (${pct(o.sl)}/day)`);
  }
  if (o.a != null) L.push(`Typical daily move (ATR): ${o.a.toFixed(2)}%`);
  if (o.w != null) L.push(`Position in 52-week range: ${Math.round(o.w)}%`);
  L.push("", `_${dateLabel(feed.d)} close · NSE_`, FOOTER);
  return L.join("\n");
}

export function digestText(feed, idx, { watch = [], notices = [] } = {}) {
  const g = feed.digest || {};
  const L = [`*NSE Cockpit · ${dateLabel(feed.d)} close*`, ""];
  if (g.breadth) L.push(`Breadth: ${g.breadth.adv} up · ${g.breadth.dec} down · ${g.breadth.unch} flat`);
  L.push(`${g.above_sma10 ?? "–"} of ${feed.n} stocks above their 10-day average`);
  L.push(`RSI ≥ 70: ${g.overbought ?? "–"} stocks · RSI ≤ 30: ${g.oversold ?? "–"}`);

  const section = (title, syms, fmt = stockLine) => {
    const rows = (syms || []).map((s) => idx.get(s)).filter(Boolean);
    if (!rows.length) return;
    L.push("", `*${title}*`, ...rows.map(fmt));
  };
  section("Top gainers", g.gainers);
  section("Top losers", g.losers);
  section("Unusual volume", g.volume, (r) => `• ${r[0]} ${r[4].toFixed(1)}× avg volume${r[2] != null ? ` (${pct(r[2])})` : ""}`);

  if (watch.length) {
    L.push("", "*Your watchlist*");
    for (const s of watch) L.push(idx.has(s) ? stockLine(idx.get(s)) : `• ${s} — no data today`);
  }
  if (notices.length) L.push("", "*🔔 Alerts triggered*", ...notices);

  L.push("", "Send a symbol (e.g. RELIANCE) for details · HELP for commands", FOOTER);
  return L.join("\n");
}

// One line for the template's {{2}} variable (variables can't contain line breaks)
export function summaryLine(feed, idx, { watch = [], alerts = 0, replyHint = true } = {}) {
  const g = feed.digest || {};
  const parts = [];
  if (g.breadth) parts.push(`Breadth ${g.breadth.adv} up / ${g.breadth.dec} down`);
  const short = (syms) => (syms || []).slice(0, 3).map((s) => idx.get(s)).filter((r) => r && r[2] != null)
    .map((r) => `${r[0]} ${pct(r[2])}`).join(", ");
  if (short(g.gainers)) parts.push(`Gainers: ${short(g.gainers)}`);
  if (short(g.losers)) parts.push(`Losers: ${short(g.losers)}`);
  const vol = (g.volume || []).slice(0, 3).map((s) => idx.get(s)).filter(Boolean).map((r) => `${r[0]} ${Math.round(r[4])}×`);
  if (vol.length) parts.push(`Heavy volume: ${vol.join(", ")}`);
  if (watch.length) {
    parts.push("Watchlist: " + watch.map((s) => {
      const r = idx.get(s);
      return r ? `${s} ${inr(r[1])}${r[2] != null ? ` (${pct(r[2])})` : ""}` : `${s} n/a`;
    }).join(", "));
  }
  if (alerts) parts.push(`${alerts} alert${alerts > 1 ? "s" : ""} triggered`);
  let line = parts.join(" · ") || `${feed.n} NSE stocks updated`;
  const hint = replyHint ? " · Reply TODAY for details" : "";
  if (line.length + hint.length > 900) line = line.slice(0, 899 - hint.length) + "…";
  return line + hint;
}

// ── Alerts ───────────────────────────────────────────────────────────────────
const KIND_LABEL = { above: "price above", below: "price below", rsi_above: "RSI above", rsi_below: "RSI below" };
export const alertLabel = (a) => `${a.symbol} ${KIND_LABEL[a.kind]} ${a.kind.startsWith("rsi") ? a.value : inr(a.value)}`;

export function alertFired(a, r) {
  if (!r) return false;
  const p = r[1], rsi = r[3];
  if (a.kind === "above") return p != null && p >= a.value;
  if (a.kind === "below") return p != null && p <= a.value;
  if (a.kind === "rsi_above") return rsi != null && rsi >= a.value;
  if (a.kind === "rsi_below") return rsi != null && rsi <= a.value;
  return false;
}

export function alertNotice(a, r) {
  return `• ${alertLabel(a)} — now ${inr(r[1])}, RSI ${r[3] != null ? Math.round(r[3]) : "–"}`;
}
