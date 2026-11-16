// Offline simulation: runs the Worker against a local SQLite "D1", the local
// output/bot.json, and fake WhatsApp/Claude APIs. Usage: node test/simulate.mjs
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { createHmac } from "node:crypto";
import worker from "../src/index.js";

const root = new URL("../../", import.meta.url);
const FEED = process.env.FEED_FILE || new URL("output/bot.json", root);

// ── Fake D1 ──
const sql = new DatabaseSync(":memory:");
sql.exec(readFileSync(new URL("schema.sql", new URL("../", import.meta.url)), "utf8"));
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

// ── Fake outbound APIs ──
const sent = [];
globalThis.fetch = async (url, init = {}) => {
  url = String(url);
  if (url.includes("raw.githubusercontent.com")) return new Response(readFileSync(FEED));
  if (url.includes("graph.facebook.com")) {
    const b = JSON.parse(init.body);
    if (b.type) sent.push(b);
    return new Response("{}");
  }
  if (url.includes("api.anthropic.com")) {
    const b = JSON.parse(init.body);
    const data = b.messages[0].content;
    return Response.json({ content: [{ type: "text", text: `[Claude stub] model=${b.model}, data has ${JSON.parse(data.split("\n")[1]).stocks.length} stocks` }] });
  }
  throw new Error("unexpected fetch " + url);
};

const env = {
  DB, FEED_REPO: "x/y", WA_TOKEN: "t", WA_PHONE_NUMBER_ID: "1", WA_VERIFY_TOKEN: "vt",
  WA_APP_SECRET: "appsecret", BROADCAST_SECRET: "bs", ANTHROPIC_API_KEY: "k", ALLOWED_NUMBERS: "919800000001",
};
const pending = [];
const ctx = { waitUntil: (p) => pending.push(p) };
const flush = async () => { await Promise.all(pending.splice(0)); };

let n = 0;
async function say(from, text) {
  const body = JSON.stringify({ entry: [{ changes: [{ value: {
    contacts: [{ wa_id: from, profile: { name: "Samarth" } }],
    messages: [{ id: `m${++n}`, from, type: "text", text: { body: text } }],
  } }] }] });
  const sig = "sha256=" + createHmac("sha256", env.WA_APP_SECRET).update(body).digest("hex");
  const res = await worker.fetch(new Request("https://bot/webhook", { method: "POST", body, headers: { "x-hub-signature-256": sig } }), env, ctx);
  await flush();
  const out = sent.splice(0);
  console.log(`\n>>> ${from}: ${text}   [HTTP ${res.status}]`);
  for (const m of out) console.log(m.type === "text" ? m.text.body : `[template ${m.template.name} ${JSON.stringify(m.template.components)}]`);
  return out;
}

// Webhook verification
const v = await worker.fetch(new Request("https://bot/webhook?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=42"), env, ctx);
console.log("verify:", v.status, await v.text());
const bad = await worker.fetch(new Request("https://bot/webhook", { method: "POST", body: "{}", headers: { "x-hub-signature-256": "sha256=00" } }), env, ctx);
console.log("bad signature:", bad.status);

const me = "919800000001";
await say(me, "hi");
await say(me, "reliance");
await say(me, "tata motors");
await say(me, "RELIANC");
await say(me, "watch tcs infy hdfcbank zzzzz");
await say(me, "alert tcs above 1");
await say(me, "alert infy rsi below 99");
await say(me, "alert tcs sideways 5");
await say(me, "list");
await say(me, "what does a high RSI on INFY mean? should I buy?");
await say(me, "today");
await say("919811111111", "hi"); // not on allow-list

// Broadcast — user is inside the 24h window → full text
const b = async (q = "") => {
  const r = await worker.fetch(new Request("https://bot/broadcast" + q, { method: "POST", headers: { authorization: "Bearer bs" }, body: JSON.stringify({ ref: "abc123" }) }), env, ctx);
  console.log("\n### broadcast", r.status, JSON.stringify(await r.json()));
  for (const m of sent.splice(0)) console.log(m.type === "text" ? m.text.body : `[template ${m.template.name} ${JSON.stringify(m.template.components)}]`);
};
await b();
await b(); // same day → skipped
// Simulate user silent for 2 days → template path
sql.prepare("UPDATE users SET last_inbound_at = ?").run(new Date(Date.now() - 2 * 864e5).toISOString());
await b("?force=1");
const unauth = await worker.fetch(new Request("https://bot/broadcast", { method: "POST" }), env, ctx);
console.log("unauthorised broadcast:", unauth.status);
await say(me, "stop");
