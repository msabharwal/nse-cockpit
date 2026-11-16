// NSE Cockpit WhatsApp bot — Cloudflare Worker
//
//   GET  /webhook    Meta webhook verification (only when using Meta directly)
//   POST /webhook    incoming WhatsApp messages (Twilio form posts or Meta JSON)
//   POST /broadcast  evening update (called by GitHub Actions after the pipeline)
//   GET  /           health check

import {
  getFeed, normSymbol, suggest, stockCard, digestText, stockLine, dateLabel, summaryLine,
  alertLabel, alertFired, alertNotice, FOOTER,
} from "./feed.js";
import { sendText, sendTemplate, hasTemplate, markRead, verifyMeta, verifyTwilio } from "./whatsapp.js";
import { askClaude } from "./claude.js";

const WINDOW_MS = 23.5 * 60 * 60 * 1000; // stay just inside WhatsApp's 24h reply window
const MAX_WATCH = 25;
const MAX_ALERTS = 15;
const now = () => new Date().toISOString();

// Old names people still type → current NSE symbols
const RENAMED = {
  TATAMOTORS: ["Tata Motors was demerged into two listed companies.", ["TMPV", "TMCV"]],
  TATAMOTOR: ["Tata Motors was demerged into two listed companies.", ["TMPV", "TMCV"]],
};

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    try {
      if (url.pathname === "/webhook" && request.method === "GET") return verifyWebhook(url, env);
      if (url.pathname === "/webhook" && request.method === "POST") return await webhook(request, env, ctx);
      if (url.pathname === "/broadcast" && request.method === "POST") return await broadcast(request, env);
      if (url.pathname === "/") return new Response("NSE Cockpit bot: ok");
      return new Response("Not found", { status: 404 });
    } catch (err) {
      console.error(err.stack || err);
      return new Response("Server error", { status: 500 });
    }
  },
};

// ── Webhook ──────────────────────────────────────────────────────────────────
function verifyWebhook(url, env) {
  const ok = url.searchParams.get("hub.mode") === "subscribe" &&
    url.searchParams.get("hub.verify_token") === env.WA_VERIFY_TOKEN;
  return ok ? new Response(url.searchParams.get("hub.challenge")) : new Response("Forbidden", { status: 403 });
}

async function webhook(request, env, ctx) {
  const raw = await request.text();
  const isTwilio = (request.headers.get("content-type") || "").includes("application/x-www-form-urlencoded");
  let messages;

  if (isTwilio) {
    const params = new URLSearchParams(raw);
    if (!(await verifyTwilio(env, request.url, params, request.headers.get("x-twilio-signature")))) {
      return new Response("Bad signature", { status: 401 });
    }
    messages = [{
      id: params.get("MessageSid"),
      phone: (params.get("From") || "").replace(/\D/g, ""),
      name: params.get("ProfileName"),
      text: (params.get("ButtonText") || params.get("Body") || "").trim(),
    }];
  } else {
    if (!(await verifyMeta(env, raw, request.headers.get("x-hub-signature-256")))) {
      return new Response("Bad signature", { status: 401 });
    }
    messages = metaMessages(JSON.parse(raw));
  }

  // Acknowledge immediately; do the work in the background
  ctx.waitUntil(processMessages(env, messages).catch((e) => console.error(e.stack || e)));
  return isTwilio
    ? new Response("<Response></Response>", { headers: { "content-type": "text/xml" } })
    : new Response("OK");
}

function metaMessages(body) {
  const out = [];
  for (const entry of body.entry || []) {
    for (const change of entry.changes || []) {
      const v = change.value || {};
      const names = Object.fromEntries((v.contacts || []).map((c) => [c.wa_id, c.profile?.name]));
      for (const m of v.messages || []) {
        out.push({
          id: m.id,
          phone: m.from,
          name: names[m.from],
          text: (m.type === "text" ? m.text?.body :
            m.type === "button" ? m.button?.text :
            m.type === "interactive" ? (m.interactive?.button_reply?.title || m.interactive?.list_reply?.title) : "")?.trim() || "",
        });
      }
    }
  }
  return out;
}

async function processMessages(env, messages) {
  for (const msg of messages) {
    if (!msg.id || !msg.phone) continue;
    const fresh = await env.DB.prepare("INSERT OR IGNORE INTO seen (id, at) VALUES (?, ?)").bind(msg.id, now()).run();
    if (!fresh.meta.changes) continue; // duplicate delivery
    await handleMessage(env, msg);
  }
}

function allowed(env, phone) {
  const list = (env.ALLOWED_NUMBERS || "").split(",").map((s) => s.replace(/\D/g, "")).filter(Boolean);
  return list.length === 0 || list.includes(phone);
}

async function handleMessage(env, msg) {
  const { phone, name, text } = msg;
  if (!allowed(env, phone)) {
    await sendText(env, phone, "Thanks for your interest! NSE Cockpit is in private beta right now.");
    return;
  }
  await markRead(env, msg.id);

  // Register / refresh the user (this also opens the 24h reply window)
  await env.DB.prepare(
    `INSERT INTO users (phone, name, subscribed, created_at, last_inbound_at) VALUES (?, ?, 1, ?, ?)
     ON CONFLICT(phone) DO UPDATE SET last_inbound_at = excluded.last_inbound_at, name = COALESCE(excluded.name, users.name)`,
  ).bind(phone, name || null, now(), now()).run();

  const reply = text ? await route(env, phone, text) : "I can only read text messages for now. Send HELP to see what I can do.";
  if (reply) await sendText(env, phone, reply);
}

// ── Commands ─────────────────────────────────────────────────────────────────
const HELP = [
  "*NSE Cockpit* — end-of-day NSE data on WhatsApp",
  "",
  "*TODAY* — today's market update",
  "*RELIANCE* — any NSE symbol for its details",
  "*WATCH TCS INFY* — add to your watchlist",
  "*UNWATCH TCS* — remove from watchlist",
  "*LIST* — your watchlist and alerts",
  "*ALERT TCS ABOVE 4200* — price alert (also BELOW)",
  "*ALERT TCS RSI BELOW 30* — RSI alert (also ABOVE)",
  "*CLEAR ALERTS* — delete all your alerts",
  "*STOP* / *START* — evening update off / on",
  "",
  "Or just ask a question, e.g. _why is INFY's RSI high?_",
  "",
  "Data updates after market close on trading days.",
  FOOTER,
].join("\n");

async function watchlistOf(env, phone) {
  const { results } = await env.DB.prepare("SELECT symbol FROM watchlist WHERE phone = ? ORDER BY symbol").bind(phone).all();
  return results.map((r) => r.symbol);
}

async function route(env, phone, text) {
  const t = text.replace(/\s+/g, " ").trim();
  const U = t.toUpperCase();
  const [cmd, ...rest] = U.split(" ");
  const args = rest.map(normSymbol).filter(Boolean);

  if (["HI", "HELLO", "HEY", "HELP", "MENU", "JOIN"].includes(U)) return HELP;

  if (U === "STOP" || U === "UNSUBSCRIBE") {
    await env.DB.prepare("UPDATE users SET subscribed = 0 WHERE phone = ?").bind(phone).run();
    return "Evening updates turned off. You can still message me any time. Send START to turn them back on.";
  }
  if (U === "START" || U === "SUBSCRIBE") {
    await env.DB.prepare("UPDATE users SET subscribed = 1 WHERE phone = ?").bind(phone).run();
    return "✅ You'll get the NSE Cockpit update after market close on trading days.\n\n" + HELP;
  }

  const { feed, idx } = await getFeed(env);

  if (["TODAY", "UPDATE", "DIGEST", "MARKET"].includes(U)) {
    const user = await env.DB.prepare("SELECT pending FROM users WHERE phone = ?").bind(phone).first();
    const notices = user?.pending ? JSON.parse(user.pending) : [];
    if (notices.length) await env.DB.prepare("UPDATE users SET pending = NULL WHERE phone = ?").bind(phone).run();
    return digestText(feed, idx, { watch: await watchlistOf(env, phone), notices });
  }

  if ((cmd === "WATCH" || cmd === "ADD") && args.length) {
    const watch = await watchlistOf(env, phone);
    const ok = [], bad = [];
    for (const s of args) (idx.has(s) ? ok : bad).push(s);
    const room = MAX_WATCH - watch.length;
    const toAdd = ok.filter((s) => !watch.includes(s)).slice(0, Math.max(room, 0));
    for (const s of toAdd) {
      await env.DB.prepare("INSERT OR IGNORE INTO watchlist (phone, symbol) VALUES (?, ?)").bind(phone, s).run();
    }
    const L = [];
    if (toAdd.length) L.push(`✅ Added: ${toAdd.join(", ")}`);
    if (ok.length > toAdd.length && room <= toAdd.length) L.push(`Watchlist limit is ${MAX_WATCH} stocks.`);
    for (const s of bad) {
      const sug = suggest(idx, s);
      L.push(`❓ ${s} not found${sug.length ? ` — did you mean ${sug.join(", ")}?` : ""}`);
    }
    return L.join("\n") || "Those are already on your watchlist.";
  }

  if ((cmd === "UNWATCH" || cmd === "REMOVE") && args.length) {
    for (const s of args) {
      await env.DB.prepare("DELETE FROM watchlist WHERE phone = ? AND symbol = ?").bind(phone, s).run();
    }
    return `Removed: ${args.join(", ")}`;
  }

  if (["LIST", "WATCHLIST", "ALERTS", "MY"].includes(U)) {
    const watch = await watchlistOf(env, phone);
    const { results: alerts } = await env.DB.prepare("SELECT * FROM alerts WHERE phone = ? ORDER BY id").bind(phone).all();
    const L = [`*Your watchlist* (${dateLabel(feed.d)} close)`];
    L.push(...(watch.length ? watch.map((s) => (idx.has(s) ? stockLine(idx.get(s)) : `• ${s} — no data`)) : ["(empty — try WATCH TCS)"]));
    L.push("", "*Your alerts*");
    L.push(...(alerts.length ? alerts.map((a) => `• ${alertLabel(a)}`) : ["(none — try ALERT TCS ABOVE 4200)"]));
    return L.join("\n");
  }

  if (U === "CLEAR ALERTS" || U === "DELETE ALERTS") {
    await env.DB.prepare("DELETE FROM alerts WHERE phone = ?").bind(phone).run();
    return "All your alerts are deleted.";
  }

  if (cmd === "ALERT") {
    // ALERT SYM ABOVE|BELOW N   or   ALERT SYM RSI ABOVE|BELOW N
    const m = U.match(/^ALERT\s+(\S+)\s+(RSI\s+)?(ABOVE|BELOW|>|<)\s+([\d.,]+)$/);
    if (!m) return "Format: *ALERT TCS ABOVE 4200* or *ALERT TCS RSI BELOW 30*";
    const sym = normSymbol(m[1]);
    if (!idx.has(sym)) {
      const sug = suggest(idx, sym);
      return `❓ ${sym} not found${sug.length ? ` — did you mean ${sug.join(", ")}?` : ""}`;
    }
    const dir = m[3] === "ABOVE" || m[3] === ">" ? "above" : "below";
    const kind = m[2] ? `rsi_${dir}` : dir;
    const value = parseFloat(m[4].replace(/,/g, ""));
    if (!isFinite(value) || value <= 0 || (m[2] && value >= 100)) return "That number doesn't look right — please check it.";
    const count = await env.DB.prepare("SELECT COUNT(*) AS n FROM alerts WHERE phone = ?").bind(phone).first();
    if (count.n >= MAX_ALERTS) return `You can have up to ${MAX_ALERTS} alerts. Send CLEAR ALERTS to start over.`;
    await env.DB.prepare("INSERT INTO alerts (phone, symbol, kind, value, created_at) VALUES (?, ?, ?, ?, ?)")
      .bind(phone, sym, kind, value, now()).run();
    const r = idx.get(sym);
    return `🔔 Alert set: ${alertLabel({ symbol: sym, kind, value })}\nLast close ${stockLine(r).slice(2)}\n\nAlerts are checked on each day's closing data and fire once.`;
  }

  // A bare symbol (or "tata motors" → TATAMOTORS)
  const asSym = normSymbol(t);
  if (idx.has(asSym)) return stockCard(idx.get(asSym), feed);
  if (RENAMED[asSym]) {
    const [note, syms] = RENAMED[asSym];
    return `${note} Send one of: ${syms.filter((s) => idx.has(s)).join(", ")}`;
  }
  if (/^[A-Z0-9&\-]{2,20}$/.test(asSym) && t.split(" ").length <= 2) {
    const sug = suggest(idx, asSym);
    if (sug.length) return `❓ ${asSym} not found. Did you mean: ${sug.join(", ")}?`;
  }

  // Anything else → Claude (if configured)
  const answer = await askClaude(env, t, { feed, idx, watch: await watchlistOf(env, phone) });
  if (answer) return answer;
  return `Sorry, I didn't understand that.\n\n${HELP}`;
}

// ── Evening broadcast ────────────────────────────────────────────────────────
async function broadcast(request, env) {
  if (!env.BROADCAST_SECRET || request.headers.get("authorization") !== `Bearer ${env.BROADCAST_SECRET}`) {
    return new Response("Unauthorized", { status: 401 });
  }
  const { ref } = await request.json().catch(() => ({}));
  if (ref) {
    await env.DB.prepare("INSERT INTO meta (k, v) VALUES ('feed_ref', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v").bind(ref).run();
  }
  const { feed, idx } = await getFeed(env, { force: true });

  // Don't send the same day's update twice (e.g. a manual re-run)
  const last = await env.DB.prepare("SELECT v FROM meta WHERE k = 'last_broadcast_d'").first();
  if (last?.v === feed.d && !new URL(request.url).searchParams.has("force")) {
    return Response.json({ skipped: true, reason: `already sent for ${feed.d}` });
  }

  const { results: users } = await env.DB.prepare(
    "SELECT * FROM users WHERE subscribed = 1 OR phone IN (SELECT DISTINCT phone FROM alerts)",
  ).all();
  const { results: watchRows } = await env.DB.prepare("SELECT phone, symbol FROM watchlist ORDER BY symbol").all();
  const { results: alerts } = await env.DB.prepare("SELECT * FROM alerts").all();

  const byPhone = (rows) => rows.reduce((m, r) => ((m[r.phone] ||= []).push(r), m), {});
  const watchBy = byPhone(watchRows), alertBy = byPhone(alerts);
  const stats = { date: feed.d, users: users.length, text: 0, template: 0, failed: 0, alerts_fired: 0 };

  for (const u of users) {
    if (!allowed(env, u.phone)) continue;
    const fired = (alertBy[u.phone] || []).filter((a) => alertFired(a, idx.get(a.symbol)));
    for (const a of fired) await env.DB.prepare("DELETE FROM alerts WHERE id = ?").bind(a.id).run();
    stats.alerts_fired += fired.length;
    const notices = [...(u.pending ? JSON.parse(u.pending) : []), ...fired.map((a) => alertNotice(a, idx.get(a.symbol)))];

    if (!u.subscribed && !fired.length) continue;
    const inWindow = u.last_inbound_at && Date.now() - Date.parse(u.last_inbound_at) < WINDOW_MS;
    const watch = (watchBy[u.phone] || []).map((w) => w.symbol);

    let ok;
    // No approved template yet (e.g. Twilio sandbox) → always try free-form text
    if (inWindow || !hasTemplate(env)) {
      const body = u.subscribed
        ? digestText(feed, idx, { watch, notices })
        : `*🔔 Alerts triggered* (${dateLabel(feed.d)} close)\n${notices.join("\n")}\n\n${FOOTER}`;
      ok = await sendText(env, u.phone, body);
      if (ok) await env.DB.prepare("UPDATE users SET pending = NULL WHERE phone = ?").bind(u.phone).run();
      stats.text++;
    } else {
      // Outside the 24h window WhatsApp only allows an approved template:
      // {{1}} date, {{2}} one-line summary. Replying TODAY gets the full update
      // (and opens the window again).
      await env.DB.prepare("UPDATE users SET pending = ? WHERE phone = ?")
        .bind(notices.length ? JSON.stringify(notices.slice(-20)) : null, u.phone).run();
      ok = await sendTemplate(env, u.phone, [
        dateLabel(feed.d),
        summaryLine(feed, idx, { watch: u.subscribed ? watch : [], alerts: notices.length }),
      ]);
      stats.template++;
    }
    if (!ok) stats.failed++;
  }

  await env.DB.prepare("INSERT INTO meta (k, v) VALUES ('last_broadcast_d', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v").bind(feed.d).run();
  // Keep the duplicate-message table small
  await env.DB.prepare("DELETE FROM seen WHERE at < ?").bind(new Date(Date.now() - 3 * 864e5).toISOString()).run();
  return Response.json(stats);
}
