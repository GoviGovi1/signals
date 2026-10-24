/**
 * Always-on command handler for @Mhosignalbot. Free Cloudflare Workers tier.
 * Telegram POSTs updates to /hook. GitHub Actions keeps the signal scan and calls
 * bot.py --serve, which broadcasts outbox lines and never calls getUpdates while
 * this webhook is set.
 *
 * KV keys: "replies" (command text from bot.py) and "ev:<ts>:<chat>:<op>:<rand>".
 * Secrets: TG_TOKEN, WEBHOOK_SECRET. Neither is logged.
 */
import FALLBACK from "../fallback.json" with { type: "json" };

const REPLY_KEYS = ["signals", "stats", "unlocks", "watchlist", "help", "welcome", "stop"];
const EVENT_KEY = /^ev:(\d{13}):(-?\d+):(start|stop):([0-9a-f]+)$/;
const MAX_EVENTS = 200;
const MAX_BODY = 200000;

export function textParts(text, limit = 4000) {
  text = text || "";
  const parts = [];
  while (text.length > limit) {
    let cut = text.lastIndexOf("\n", limit);
    if (cut < limit / 2) cut = limit;
    parts.push(text.slice(0, cut));
    text = text.slice(cut).replace(/^\n/, "");
  }
  if (text) parts.push(text);
  return parts.length ? parts : [""];
}

export function commandOf(text) {
  const t = String(text || "").trim();
  if (!t.startsWith("/")) return "";
  return t.split(/\s+/)[0].split("@")[0].toLowerCase();
}

/** Same command map as bot.py handle(). Null means ignore (a group chat that did not send a command). */
export function replyKey(cmd, chatType) {
  if (cmd === "/start") return "welcome";
  if (cmd === "/stop") return "stop";
  if (cmd === "/signals") return "signals";
  if (cmd === "/stats") return "stats";
  if (cmd === "/unlocks") return "unlocks";
  if (cmd === "/watchlist" || cmd === "/catalysts") return "watchlist";
  if (cmd === "/help" || chatType === "private") return "help";
  return null;
}

export function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  const x = new TextEncoder().encode(a);
  const y = new TextEncoder().encode(b);
  if (x.length !== y.length || x.length === 0) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

function ready(env) {
  return !!(env && env.SUBS && env.TG_TOKEN && env.WEBHOOK_SECRET);
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function bearer(req) {
  const h = req.headers.get("Authorization") || "";
  return h.startsWith("Bearer ") ? h.slice(7) : "";
}

function authed(req, env) {
  return safeEqual(bearer(req), env.WEBHOOK_SECRET || "");
}

export function parseEvent(name, metadata) {
  const meta = metadata || {};
  if ((meta.op === "start" || meta.op === "stop") && meta.chat != null && meta.ts != null) {
    return {
      key: name,
      op: meta.op,
      chat: String(meta.chat),
      joined: Number(meta.joined) || Math.floor(Number(meta.ts) / 1000),
      type: meta.type || "private",
      ts: Number(meta.ts),
    };
  }
  const m = EVENT_KEY.exec(name || "");
  if (!m) return null;
  return {
    key: name,
    op: m[3],
    chat: m[2],
    joined: Math.floor(Number(m[1]) / 1000),
    type: "private",
    ts: Number(m[1]),
  };
}

async function readBody(req) {
  const raw = await req.text();
  if (raw.length > MAX_BODY) return { error: json({ ok: false }, 413) };
  try {
    return { value: raw ? JSON.parse(raw) : {} };
  } catch {
    return { error: json({ ok: false }, 400) };
  }
}

async function listEvents(env) {
  const events = [];
  let cursor;
  for (let page = 0; page < 5 && events.length < MAX_EVENTS; page++) {
    const list = await env.SUBS.list({ prefix: "ev:", cursor, limit: 1000 });
    for (const key of list.keys || []) {
      const ev = parseEvent(key.name, key.metadata);
      if (ev) events.push(ev);
    }
    if (list.list_complete || !list.cursor) break;
    cursor = list.cursor;
  }
  events.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
  return events.slice(0, MAX_EVENTS);
}

async function replyText(env, key) {
  let bundle = null;
  try {
    const raw = await env.SUBS.get("replies", { cacheTtl: 30 });
    if (raw) bundle = JSON.parse(raw);
  } catch {
    bundle = null;
  }
  if (bundle && typeof bundle[key] === "string" && bundle[key]) return bundle[key];
  if (env.REPLIES_URL) {
    try {
      const res = await fetch(env.REPLIES_URL, { cf: { cacheTtl: 60 } });
      if (res.ok) {
        bundle = await res.json();
        if (bundle && typeof bundle[key] === "string" && bundle[key]) return bundle[key];
      }
    } catch {
      /* site copy is optional */
    }
  }
  return FALLBACK[key] || FALLBACK.help;
}

async function sendText(env, chat, text) {
  for (const part of textParts(text)) {
    let res;
    try {
      res = await fetch(`https://api.telegram.org/bot${env.TG_TOKEN}/sendMessage`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ chat_id: chat, text: part, disable_web_page_preview: true }),
      });
    } catch {
      const err = new Error("telegram");
      err.code = 0;
      throw err;
    }
    let data = {};
    try {
      data = await res.json();
    } catch {
      data = {};
    }
    if (!data.ok) {
      const code = data.error_code || res.status;
      if (code === 403 || code === 400) return;
      const err = new Error("telegram");
      err.code = code;
      throw err;
    }
  }
}

function eventKey(op, chat) {
  const ts = String(Date.now()).padStart(13, "0");
  const rand = crypto.randomUUID().replace(/-/g, "").slice(0, 8);
  return `ev:${ts}:${chat}:${op}:${rand}`;
}

async function record(env, op, msg) {
  const chat = String(msg.chat.id);
  const ts = Date.now();
  const key = eventKey(op, chat);
  await env.SUBS.put(key, "1", {
    metadata: {
      op,
      chat,
      joined: Math.floor(ts / 1000),
      type: msg.chat.type || "private",
      ts,
    },
  });
}

async function hook(req, env) {
  const got = req.headers.get("X-Telegram-Bot-Api-Secret-Token") || "";
  if (!safeEqual(got, env.WEBHOOK_SECRET || "")) return new Response("unauthorized", { status: 401 });
  const body = await readBody(req);
  if (body.error) return body.error;
  const msg = body.value && body.value.message;
  if (!msg || !msg.text || !msg.chat || msg.chat.id == null) return new Response("ok");
  const cmd = commandOf(msg.text);
  const key = replyKey(cmd, msg.chat.type || "private");
  if (!key) return new Response("ok");
  try {
    if (cmd === "/start" || cmd === "/stop") await record(env, cmd === "/start" ? "start" : "stop", msg);
    await sendText(env, String(msg.chat.id), await replyText(env, key));
  } catch (e) {
    console.log("hook failed", e && e.code ? e.code : "");
    return new Response("error", { status: 500 });
  }
  return new Response("ok");
}

async function admin(req, env, url) {
  if (!authed(req, env)) return new Response("unauthorized", { status: 401 });
  if (req.method === "GET" && url.pathname === "/admin/events") {
    return json({ events: await listEvents(env) });
  }
  if (req.method === "POST" && url.pathname === "/admin/events/ack") {
    const body = await readBody(req);
    if (body.error) return body.error;
    const keys = Array.isArray(body.value.keys) ? body.value.keys.slice(0, 40) : [];
    const deleted = [];
    for (const key of keys) {
      if (typeof key === "string" && key.startsWith("ev:") && key.length < 200) {
        await env.SUBS.delete(key);
        deleted.push(key);
      }
    }
    return json({ ok: true, deleted });
  }
  if (req.method === "POST" && url.pathname === "/admin/replies") {
    const body = await readBody(req);
    if (body.error) return body.error;
    const bundle = {};
    for (const key of REPLY_KEYS) {
      if (typeof body.value[key] !== "string" || !body.value[key]) return json({ ok: false }, 400);
      bundle[key] = body.value[key];
    }
    await env.SUBS.put("replies", JSON.stringify(bundle));
    return json({ ok: true });
  }
  return new Response("not found", { status: 404 });
}

export async function handle(req, env) {
  const url = new URL(req.url);
  if (req.method === "GET" && url.pathname === "/health") {
    return json({ ok: ready(env) }, ready(env) ? 200 : 503);
  }
  if (!ready(env)) return json({ ok: false }, 503);
  if (url.pathname === "/hook") {
    if (req.method !== "POST") return new Response("not found", { status: 404 });
    return hook(req, env);
  }
  if (url.pathname.startsWith("/admin/")) return admin(req, env, url);
  return new Response("not found", { status: 404 });
}

export default { fetch: (req, env) => handle(req, env) };
