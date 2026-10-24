import assert from "node:assert/strict";
import { commandOf, handle, parseEvent, replyKey, textParts } from "./src/index.js";

const SECRET = "abcDEF123_secret-token";
const TOKEN = "000000:TEST";

function mockKV() {
  const values = new Map();
  const meta = new Map();
  return {
    async get(key) {
      return values.has(key) ? values.get(key) : null;
    },
    async put(key, value, opts) {
      values.set(key, value);
      if (opts && opts.metadata) meta.set(key, opts.metadata);
    },
    async delete(key) {
      values.delete(key);
      meta.delete(key);
    },
    async list({ prefix } = {}) {
      const keys = [...values.keys()].filter((name) => !prefix || name.startsWith(prefix)).sort();
      return {
        keys: keys.map((name) => ({ name, metadata: meta.get(name) || null })),
        list_complete: true,
      };
    },
    _values: values,
    _meta: meta,
  };
}

function env(kv, extra = {}) {
  return { SUBS: kv, TG_TOKEN: TOKEN, WEBHOOK_SECRET: SECRET, REPLIES_URL: "", ...extra };
}

const sent = [];
globalThis.fetch = async (url, opts) => {
  const u = String(url);
  if (u.includes("api.telegram.org")) {
    sent.push(JSON.parse(opts.body));
    if (u.includes(TOKEN) === false) throw new Error("token leaked into an unexpected fetch");
    return new Response(JSON.stringify({ ok: true, result: { message_id: 1 } }), { status: 200 });
  }
  if (u.includes("replies.json")) {
    return new Response(JSON.stringify({ signals: "from-site", help: "from-site-help", welcome: "from-site-welcome", stop: "from-site-stop", stats: "from-site-stats", unlocks: "from-site-unlocks", watchlist: "from-site-watch" }), { status: 200 });
  }
  throw new Error("unexpected fetch");
};

function req(path, { method = "GET", headers = {}, body } = {}) {
  return new Request("https://bot.example" + path, {
    method,
    headers,
    body: body == null ? undefined : JSON.stringify(body),
  });
}

function hook(text, chat = { id: 42, type: "private" }, secret = SECRET) {
  return req("/hook", {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": secret },
    body: { update_id: 1, message: { message_id: 1, chat, text, date: 1 } },
  });
}

const tests = [];
function test(name, fn) {
  tests.push([name, fn]);
}

test("text parts stay under the limit", () => {
  const parts = textParts("a".repeat(50) + "\n" + "b".repeat(50), 60);
  assert.ok(parts.every((p) => p.length <= 60));
  assert.equal(parts.join(""), "a".repeat(50) + "b".repeat(50));
  assert.deepEqual(textParts("short"), ["short"]);
});

test("commands match the python bot", () => {
  assert.equal(commandOf("/Signals@Mhosignalbot now"), "/signals");
  assert.equal(replyKey("/start", "private"), "welcome");
  assert.equal(replyKey("/stop", "group"), "stop");
  assert.equal(replyKey("/catalysts", "private"), "watchlist");
  assert.equal(replyKey("/help", "group"), "help");
  assert.equal(replyKey("", "private"), "help");
  assert.equal(replyKey("", "group"), null);
  assert.equal(replyKey("/nope", "supergroup"), null);
});

test("health hides secrets and is down until configured", async () => {
  const down = await handle(req("/health"), {});
  assert.equal(down.status, 503);
  const up = await handle(req("/health"), env(mockKV()));
  assert.equal(up.status, 200);
  const body = await up.json();
  assert.equal(body.ok, true);
  assert.equal(JSON.stringify(body).includes(SECRET), false);
  assert.equal(JSON.stringify(body).includes(TOKEN), false);
});

test("hook rejects a missing or wrong secret", async () => {
  const kv = mockKV();
  assert.equal((await handle(hook("/start", { id: 1, type: "private" }, ""), env(kv))).status, 401);
  assert.equal((await handle(hook("/start", { id: 1, type: "private" }, "nope"), env(kv))).status, 401);
  assert.equal(kv._values.size, 0);
});

test("/start subscribes, replies, and does not echo the token", async () => {
  sent.length = 0;
  const kv = mockKV();
  await kv.put("replies", JSON.stringify({
    welcome: "WELCOME", signals: "SIG", stats: "ST", unlocks: "UN", watchlist: "WA", help: "HE", stop: "STOP",
  }));
  const res = await handle(hook("/start@Mhosignalbot"), env(kv));
  assert.equal(res.status, 200);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].chat_id, "42");
  assert.equal(sent[0].text, "WELCOME");
  const events = [...kv._meta.entries()];
  assert.equal(events.length, 1);
  assert.equal(events[0][1].op, "start");
  assert.equal(events[0][1].chat, "42");
  assert.equal(String(res.url || "").includes(TOKEN), false);
});

test("/signals uses the stored reply and /stop records a leave", async () => {
  sent.length = 0;
  const kv = mockKV();
  await kv.put("replies", JSON.stringify({
    welcome: "W", signals: "OPEN", stats: "S", unlocks: "U", watchlist: "L", help: "H", stop: "BYE",
  }));
  assert.equal((await handle(hook("/signals"), env(kv))).status, 200);
  assert.equal(sent.at(-1).text, "OPEN");
  assert.equal((await handle(hook("/stop"), env(kv))).status, 200);
  assert.equal(sent.at(-1).text, "BYE");
  const ops = [...kv._meta.values()].map((m) => m.op);
  assert.deepEqual(ops, ["stop"]);
});

test("private chatter gets help; a group is ignored", async () => {
  sent.length = 0;
  const kv = mockKV();
  await kv.put("replies", JSON.stringify({
    welcome: "W", signals: "S", stats: "S", unlocks: "U", watchlist: "L", help: "HELP", stop: "BYE",
  }));
  await handle(hook("hello"), env(kv));
  assert.equal(sent.at(-1).text, "HELP");
  const n = sent.length;
  const res = await handle(hook("hello", { id: -100, type: "group" }), env(kv));
  assert.equal(res.status, 200);
  assert.equal(sent.length, n);
  assert.equal(kv._meta.size, 0);
});

test("admin lists join events and ack deletes only ev keys", async () => {
  const kv = mockKV();
  const e = env(kv);
  await handle(hook("/start", { id: -1005, type: "group" }), e);
  await kv.put("replies", "{}");
  const denied = await handle(req("/admin/events"), e);
  assert.equal(denied.status, 401);
  const listed = await handle(req("/admin/events", { headers: { Authorization: "Bearer " + SECRET } }), e);
  const body = await listed.json();
  assert.equal(body.events.length, 1);
  assert.equal(body.events[0].chat, "-1005");
  assert.equal(body.events[0].op, "start");
  assert.equal(body.events[0].type, "group");
  const key = body.events[0].key;
  const ack = await handle(req("/admin/events/ack", {
    method: "POST",
    headers: { Authorization: "Bearer " + SECRET },
    body: { keys: [key, "replies", "ev-nope"] },
  }), e);
  const ackBody = await ack.json();
  assert.deepEqual(ackBody.deleted, [key]);
  assert.equal(kv._values.has(key), false);
  assert.equal(kv._values.has("replies"), true);
});

test("replies upload then /stats reads it; site file is the backup", async () => {
  const kv = mockKV();
  const e = env(kv, { REPLIES_URL: "https://example.invalid/replies.json" });
  const up = await handle(req("/admin/replies", {
    method: "POST",
    headers: { Authorization: "Bearer " + SECRET },
    body: { signals: "S", stats: "STATS", unlocks: "U", watchlist: "W", help: "H", welcome: "HI", stop: "BYE" },
  }), e);
  assert.equal((await up.json()).ok, true);
  sent.length = 0;
  await handle(hook("/stats"), e);
  assert.equal(sent.at(-1).text, "STATS");

  const bare = mockKV();
  sent.length = 0;
  await handle(hook("/signals"), env(bare, { REPLIES_URL: "https://example.invalid/replies.json" }));
  assert.equal(sent.at(-1).text, "from-site");
});

test("a long reply is split into several sends", async () => {
  sent.length = 0;
  const kv = mockKV();
  const long = Array.from({ length: 50 }, (_, i) => "line " + i + " " + "x".repeat(100)).join("\n");
  await kv.put("replies", JSON.stringify({
    welcome: "W", signals: long, stats: "S", unlocks: "U", watchlist: "L", help: "H", stop: "BYE",
  }));
  await handle(hook("/signals"), env(kv));
  assert.ok(sent.length > 1);
  assert.ok(sent.every((m) => m.text.length <= 4000));
  assert.equal(sent.map((m) => m.text).join("\n"), long);
});

test("event keys parse without metadata", () => {
  const ev = parseEvent("ev:1730000000000:-99:stop:ab12cd34", null);
  assert.equal(ev.op, "stop");
  assert.equal(ev.chat, "-99");
  assert.equal(ev.ts, 1730000000000);
});

test("unknown paths 404", async () => {
  assert.equal((await handle(req("/admin/nope", { headers: { Authorization: "Bearer " + SECRET } }), env(mockKV()))).status, 404);
  assert.equal((await handle(req("/nope"), env(mockKV()))).status, 404);
});

let failed = 0;
for (const [name, fn] of tests) {
  try {
    await fn();
    console.log("ok", name);
  } catch (e) {
    failed++;
    console.error("FAIL", name);
    console.error(e);
  }
}
if (failed) process.exit(1);
console.log(tests.length + " worker tests passed");
