// WhatsApp Business Cloud API helpers (Meta Graph API)

function endpoint(env) {
  return `https://graph.facebook.com/${env.GRAPH_VERSION || "v21.0"}/${env.WA_PHONE_NUMBER_ID}/messages`;
}

async function post(env, payload) {
  const res = await fetch(endpoint(env), {
    method: "POST",
    headers: { Authorization: `Bearer ${env.WA_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify({ messaging_product: "whatsapp", ...payload }),
  });
  if (!res.ok) {
    console.error("WhatsApp API error", res.status, await res.text());
    return false;
  }
  return true;
}

export function sendText(env, to, body) {
  if (body.length > 4000) body = body.slice(0, 3990) + "\n…";
  return post(env, { to, type: "text", text: { body, preview_url: false } });
}

// Template messages are required to start a conversation outside the 24h window
export function sendTemplate(env, to, params = []) {
  return post(env, {
    to,
    type: "template",
    template: {
      name: env.WA_TEMPLATE_NAME || "daily_market_update",
      language: { code: env.WA_TEMPLATE_LANG || "en" },
      components: params.length
        ? [{ type: "body", parameters: params.map((text) => ({ type: "text", text })) }]
        : [],
    },
  });
}

export function markRead(env, messageId) {
  return post(env, { status: "read", message_id: messageId });
}

// Verifies X-Hub-Signature-256 so only Meta can call the webhook
export async function verifySignature(env, rawBody, header) {
  if (!env.WA_APP_SECRET) {
    console.warn("WA_APP_SECRET not set — skipping webhook signature check");
    return true;
  }
  if (!header?.startsWith("sha256=")) return false;
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(env.WA_APP_SECRET),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(rawBody));
  const hex = [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
  const given = header.slice(7);
  if (given.length !== hex.length) return false;
  let diff = 0;
  for (let i = 0; i < hex.length; i++) diff |= hex.charCodeAt(i) ^ given.charCodeAt(i);
  return diff === 0;
}
