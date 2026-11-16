// WhatsApp transport. Twilio by default; Meta's Cloud API is kept as an option.
//   WA_PROVIDER = "twilio" (default when TWILIO_ACCOUNT_SID is set) | "meta"

export const provider = (env) =>
  (env.WA_PROVIDER || (env.TWILIO_ACCOUNT_SID ? "twilio" : "meta")).toLowerCase();

const digits = (s) => String(s || "").replace(/\D/g, "");

// Split long text on line boundaries (Twilio allows 1,600 chars per WhatsApp message)
export function chunks(text, limit) {
  const out = [];
  let cur = "";
  for (const line of text.split("\n")) {
    if (cur && cur.length + line.length + 1 > limit) { out.push(cur.trimEnd()); cur = ""; }
    cur += line + "\n";
  }
  if (cur.trim()) out.push(cur.trimEnd());
  return out;
}

// ── Twilio ───────────────────────────────────────────────────────────────────
async function twilioPost(env, fields) {
  const sid = env.TWILIO_ACCOUNT_SID;
  const res = await fetch(`https://api.twilio.com/2010-04-01/Accounts/${sid}/Messages.json`, {
    method: "POST",
    headers: {
      Authorization: "Basic " + btoa(`${sid}:${env.TWILIO_AUTH_TOKEN}`),
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams({ From: `whatsapp:+${digits(env.TWILIO_WHATSAPP_FROM)}`, ...fields }),
  });
  if (!res.ok) {
    console.error("Twilio API error", res.status, await res.text());
    return false;
  }
  return true;
}

// ── Meta Cloud API ───────────────────────────────────────────────────────────
async function metaPost(env, payload) {
  const res = await fetch(
    `https://graph.facebook.com/${env.GRAPH_VERSION || "v21.0"}/${env.WA_PHONE_NUMBER_ID}/messages`,
    {
      method: "POST",
      headers: { Authorization: `Bearer ${env.WA_TOKEN}`, "Content-Type": "application/json" },
      body: JSON.stringify({ messaging_product: "whatsapp", ...payload }),
    },
  );
  if (!res.ok) {
    console.error("WhatsApp API error", res.status, await res.text());
    return false;
  }
  return true;
}

// ── Public API ───────────────────────────────────────────────────────────────
export async function sendText(env, to, body) {
  if (provider(env) === "twilio") {
    let ok = true;
    for (const part of chunks(body, 1600)) ok = (await twilioPost(env, { To: `whatsapp:+${digits(to)}`, Body: part })) && ok;
    return ok;
  }
  let allOk = true;
  for (const part of chunks(body, 4000)) {
    allOk = (await metaPost(env, { to: digits(to), type: "text", text: { body: part, preview_url: false } })) && allOk;
  }
  return allOk;
}

// Is an approved template configured? (needed to reach people outside the 24h window)
export const hasTemplate = (env) =>
  provider(env) === "twilio" ? Boolean(env.TWILIO_CONTENT_SID) : Boolean(env.WA_TEMPLATE_NAME);

// Template variables: {{1}} = date, {{2}} = one-line summary
export function sendTemplate(env, to, params = []) {
  if (provider(env) === "twilio") {
    const vars = Object.fromEntries(params.map((v, i) => [String(i + 1), v]));
    return twilioPost(env, {
      To: `whatsapp:+${digits(to)}`,
      ContentSid: env.TWILIO_CONTENT_SID,
      ContentVariables: JSON.stringify(vars),
    });
  }
  return metaPost(env, {
    to: digits(to),
    type: "template",
    template: {
      name: env.WA_TEMPLATE_NAME,
      language: { code: env.WA_TEMPLATE_LANG || "en" },
      components: params.length
        ? [{ type: "body", parameters: params.map((text) => ({ type: "text", text })) }]
        : [],
    },
  });
}

export function markRead(env, messageId) {
  if (provider(env) !== "meta") return Promise.resolve(true); // Twilio: not needed
  return metaPost(env, { status: "read", message_id: messageId });
}

// ── Webhook signature checks ────────────────────────────────────────────────
async function hmac(algo, secret, data) {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret), { name: "HMAC", hash: algo }, false, ["sign"],
  );
  return new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(data)));
}

function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// Meta: X-Hub-Signature-256 = sha256=<hex HMAC-SHA256(app secret, raw body)>
export async function verifyMeta(env, rawBody, header) {
  if (!env.WA_APP_SECRET) {
    console.warn("WA_APP_SECRET not set — skipping Meta signature check");
    return true;
  }
  if (!header?.startsWith("sha256=")) return false;
  const sig = await hmac("SHA-256", env.WA_APP_SECRET, rawBody);
  const hex = [...sig].map((b) => b.toString(16).padStart(2, "0")).join("");
  return safeEqual(hex, header.slice(7));
}

// Twilio: X-Twilio-Signature = base64(HMAC-SHA1(auth token, URL + sorted key/value pairs))
export async function verifyTwilio(env, url, params, header) {
  if (!header) return false;
  const data = url + [...params.keys()].sort().map((k) => k + params.get(k)).join("");
  const sig = await hmac("SHA-1", env.TWILIO_AUTH_TOKEN, data);
  return safeEqual(btoa(String.fromCharCode(...sig)), header);
}
