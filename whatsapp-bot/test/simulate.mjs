// Offline simulation: runs the Worker against a local SQLite "D1", a local
// bot.json, and fake Twilio / Meta / Claude APIs.
//   node test/simulate.mjs                 (Twilio, the default)
//   PROVIDER=meta node test/simulate.mjs
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { createHmac } from "node:crypto";
import worker from "../src/index.js";

const PROVIDER = process.env.PROVIDER || "twilio";
const FEED = process.env.FEED_FILE || new URL("../../output/bot.json", import.meta.url);

// ── Fake D1 ──
const sql = new DatabaseSync(":memory:");
sql.exec(readFileSync(new URL("../schema.sql", import.meta.url), "utf8"));
const DB = {
  prepare(q) {
    let params = [];
    const st = sql.prepare(q);
    const api = {
      bind: (...p) => ((params = p), api),
      first: async () => st.get(...params) ?? null,
      all: async () => ({ results: st.all(...params) }),
      run: async () => ({ meta: { changes: st.run(...params).changes } }),
    };
    return api;
  },
};

// ── Fake outbound APIs ── (sent = normalised {to, text} or {to, template})
const sent = [];
globalThis.fetch = async (url, init = {}) => {
  url = String(url);
  if (url.includes("raw.githubusercontent.com")) return new Response(readFileSync(FEED));
  if (url.includes("api.twilio.com")) {
    const f = new URLSearchParams(init.body);
    if (!init.headers.Authorization.startsWith("Basic ")) throw new Error("no auth");
    sent.push(f.get("ContentSid")
      ? { to: f.get("To"), from: f.get("From"), template: f.get("ContentSid"), vars: JSON.parse(f.get("ContentVariables")) }
      : { to: f.get("To"), from: f.get("From"), text: f.get("Body") });
    return Response.json({ sid: "SM1" }, { status: 201 });
  }
  if (url.includes("graph.facebook.com")) {
    const b = JSON.parse(init.body);
    if (b.type === "text") sent.push({ to: b.to, text: b.text.body });
    if (b.type === "template") sent.push({ to: b.to, template: b.template.name, vars: b.template.components });
    return new Response("{}");
  }
  if (url.includes("api.anthropic.com")) {
    const b = JSON.parse(init.body);
    const data = JSON.parse(b.messages[0].content.split("\n")[1]);
    return Response.json({ content: [{ type: "text", text: `[Claude stub] model=${b.model}, data has ${data.stocks.length} stocks` }] });
  }
  throw new Error("unexpected fetch " + url);
};

const env = {
  DB, WA_PROVIDER: PROVIDER, FEED_REPO: "x/y", BROADCAST_SECRET: "bs", ANTHROPIC_API_KEY: "k",
  ALLOWED_NUMBERS: "919800000001",
  // Twilio
  TWILIO_ACCOUNT_SID: "AC123", TWILIO_AUTH_TOKEN: "tok", TWILIO_WHATSAPP_FROM: "+1 415 523 8886",
  // Meta
  WA_TOKEN: "t", WA_PHONE_NUMBER_ID: "1", WA_VERIFY_TOKEN: "vt", WA_APP_SECRET: "appsecret",
};
const pending = [];
const ctx = { waitUntil: (p) => pending.push(p) };
const flush = async () => { await Promise.all(pending.splice(0)); };
const show = (m) => console.log(m.text ?? `[template ${m.template} → ${m.to}] ${JSON.stringify(m.vars)}`);

let n = 0;
const HOOK = "https://bot.example.workers.dev/webhook";
function inbound(from, text) {
  const id = `m${++n}`;
  if (PROVIDER === "twilio") {
    const params = new URLSearchParams({
      SmsMessageSid: id, MessageSid: id, AccountSid: "AC123", From: `whatsapp:+${from}`,
      To: "whatsapp:+14155238886", Body: text, ProfileName: "Samarth", NumMedia: "0", WaId: from,
    });
    const data = HOOK + [...params.keys()].sort().map((k) => k + params.get(k)).join("");
    const sig = createHmac("sha1", env.TWILIO_AUTH_TOKEN).update(data).digest("base64");
    return new Request(HOOK, { method: "POST", body: params.toString(),
      headers: { "content-type": "application/x-www-form-urlencoded", "x-twilio-signature": sig } });
  }
  const body = JSON.stringify({ entry: [{ changes: [{ value: {
    contacts: [{ wa_id: from, profile: { name: "Samarth" } }],
    messages: [{ id, from, type: "text", text: { body: text } }],
  } }] }] });
  const sig = "sha256=" + createHmac("sha256", env.WA_APP_SECRET).update(body).digest("hex");
  return new Request(HOOK, { method: "POST", body, headers: { "content-type": "application/json", "x-hub-signature-256": sig } });
}

async function say(from, text) {
  const res = await worker.fetch(inbound(from, text), env, ctx);
  await flush();
  console.log(`\n>>> ${from}: ${text}   [HTTP ${res.status} ${res.headers.get("content-type")}]`);
  const out = sent.splice(0);
  out.forEach(show);
  return out;
}

console.log(`=== provider: ${PROVIDER} ===`);
// Bad signature is rejected
const badReq = inbound("919800000001", "hi");
const forged = new Request(badReq, { headers: new Headers({ ...Object.fromEntries(badReq.headers), "x-twilio-signature": "AAAA", "x-hub-signature-256": "sha256=00" }) });
console.log("forged signature:", (await worker.fetch(forged, env, ctx)).status);

const me = "919800000001";
await say(me, "hi");
const card = await say(me, "reliance");
if (PROVIDER === "twilio" && card[0].to !== "whatsapp:+919800000001") throw new Error("bad To " + card[0].to);
if (PROVIDER === "twilio" && card[0].from !== "whatsapp:+14155238886") throw new Error("bad From " + card[0].from);
await say(me, "tata motors");
await say(me, "watch tcs infy tmpv zzzzz");
await say(me, "alert tcs above 1");
await say(me, "what does a high RSI on INFY mean? should I buy?");
await say("919811111111", "hi"); // not on allow-list

const broadcast = async (q = "") => {
  const r = await worker.fetch(new Request("https://bot/broadcast" + q, { method: "POST",
    headers: { authorization: "Bearer bs" }, body: JSON.stringify({ ref: "abc123" }) }), env, ctx);
  console.log("\n### broadcast", q, r.status, JSON.stringify(await r.json()));
  const out = sent.splice(0);
  out.forEach((m) => (m.text ? console.log(`[text, ${m.text.length} chars]`) : show(m)));
  return out;
};
await broadcast();                     // in 24h window → full text
await broadcast();                     // same day → skipped
sql.prepare("UPDATE users SET last_inbound_at = ?").run(new Date(Date.now() - 2 * 864e5).toISOString());
await broadcast("?force=1");           // outside window, no template configured → free-form fallback
env.TWILIO_CONTENT_SID = "HX123"; env.WA_TEMPLATE_NAME = "daily_market_update";
sql.prepare("INSERT INTO alerts (phone, symbol, kind, value, created_at) VALUES ('919800000001','INFY','above',1,'x')").run();
const t = await broadcast("?force=1"); // outside window with template → template + pending alert
if (!t[0]?.template) throw new Error("expected template");
await say(me, "today");                // pending alert is shown
console.log("unauthorised broadcast:", (await worker.fetch(new Request("https://bot/broadcast", { method: "POST" }), env, ctx)).status);
console.log("\nALL CHECKS PASSED");
