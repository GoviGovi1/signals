"""Telegram bot for the signal service (@Mhosignalbot).
Token: env SIGNALS_TG_TOKEN, else ALTSPOT_TELEGRAM_BOT_TOKEN. Never printed or written to disk.
  python3 bot.py            long-polling loop. Do not run this while a webhook is set.
  python3 bot.py --once     one polling pass: getUpdates, answer commands, broadcast, exit
  python3 bot.py --serve    Actions entry. If SIGNALS_WEBHOOK_URL + SIGNALS_WEBHOOK_SECRET are set and
                            the worker /health responds, point the Telegram webhook at the worker and
                            only broadcast (no getUpdates). Otherwise same as --once.
  python3 bot.py --push     send any unsent outbox lines once, then exit
  python3 bot.py --webhook-off   deleteWebhook so polling can be used again (does not drop updates)
  python3 bot.py --status   show bot username, subscriber count, unsent lines
Optional: private/telegram.json {"channel": "@yourchannel"} also posts broadcasts to a channel
(the bot must be an admin there). Subscribers live in private/subscribers.json (never published).
The worker (worker/) answers commands; this process still renders the reply text and sends signals."""
import fcntl, hashlib, json, os, re, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone
import config as C

SUBS = os.path.join(C.PRIVATE_DIR, "subscribers.json")
STATE = os.path.join(C.PRIVATE_DIR, "bot_state.json")
CFG = os.path.join(C.PRIVATE_DIR, "telegram.json")
LOG = os.path.join(C.PRIVATE_DIR, "bot.log")
REPLIES = os.path.join(C.SITE_DIR, "bot", "replies.json")
DISCLAIMER = ("Not financial advice. These are paper-tracked signals: nothing is traded for you. "
              "You can lose money, and leverage makes losses bigger and faster.")
STOP_TEXT = "You won't get signals anymore. Send /start to subscribe again."
REPLY_KEYS = ("signals", "stats", "unlocks", "watchlist", "help", "welcome", "stop")
TOMBSTONE_TTL = 86400 * 1000          # milliseconds; event timestamps are unix ms
SECRET_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")
ACK_BATCH = 30


def now_ms():
    return int(time.time() * 1000)


def token():
    t = (os.environ.get("SIGNALS_TG_TOKEN") or "").strip() or (open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "private", "tg_token")).read().strip() if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "private", "tg_token")) else None) or os.environ.get("ALTSPOT_TELEGRAM_BOT_TOKEN")
    if not t:
        sys.exit("bot: no token. Set SIGNALS_TG_TOKEN (from @BotFather).")
    return t


def log(msg):
    with open(LOG, "a") as fh:
        fh.write(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


class TGError(Exception):
    def __init__(self, code, desc):
        super().__init__(f"telegram {code}: {desc}")
        self.code = code


def _api_result(method, data, headers, timeout):
    url = f"https://api.telegram.org/bot{token()}/{method}"
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=timeout)
        d = json.load(r)
    except urllib.error.HTTPError as e:                  # never let the URL (token) reach a log
        try:
            d = json.load(e)
        except ValueError:
            raise TGError(e.code, "http error") from None
    except OSError as e:
        raise TGError(0, type(e).__name__) from None
    if not d.get("ok"):
        raise TGError(d.get("error_code"), d.get("description", "")[:120])
    return d["result"]


def api(method, params=None, timeout=20):
    return _api_result(method, urllib.parse.urlencode(params or {}).encode(), {}, timeout)


def api_json(method, payload, timeout=20):
    """JSON body so booleans stay booleans (form encoding can turn false into true)."""
    return _api_result(method, json.dumps(payload).encode(), {"Content-Type": "application/json"}, timeout)


def jload(path, default):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return default


def jsave(path, obj):
    tmp = path + ".tmp"
    json.dump(obj, open(tmp, "w"), indent=1)
    os.replace(tmp, path)


def text_parts(text, limit=4000):
    """Split a Telegram message on newlines so a long /signals reply still sends."""
    text = text or ""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts or [""]


def send(chat, text):
    last = None
    for part in text_parts(text):
        last = api("sendMessage", {"chat_id": chat, "text": part, "disable_web_page_preview": "true"})
    return last


def send_photo(chat, photo, cap, uploaded=False):
    """sendPhoto: upload the PNG the first time (multipart), then reuse Telegram's file_id."""
    if uploaded:
        return api("sendPhoto", {"chat_id": chat, "photo": photo, "caption": cap})
    import uuid
    bnd = "----mho" + uuid.uuid4().hex
    parts = []
    for k, v in (("chat_id", chat), ("caption", cap)):
        parts.append(f"--{bnd}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    parts.append(f"--{bnd}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"signal.png\"\r\n"
                 f"Content-Type: image/png\r\n\r\n".encode() + open(photo, "rb").read() + b"\r\n")
    parts.append(f"--{bnd}--\r\n".encode())
    url = f"https://api.telegram.org/bot{token()}/sendPhoto"
    req = urllib.request.Request(url, data=b"".join(parts), headers={"Content-Type": f"multipart/form-data; boundary={bnd}"})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:                  # never let the URL (token) reach a log
        try:
            d = json.load(e)
        except ValueError:
            raise TGError(e.code, "http error") from None
    except OSError as e:
        raise TGError(0, type(e).__name__) from None
    if not d.get("ok"):
        raise TGError(d.get("error_code"), d.get("description", "")[:120])
    return d["result"]


def plan_text(s=None):
    w = (s or {}).get("w") if s else C.V4.get("exit_weights")
    if not w or abs(w[0] - 1 / 3) < 0.01:
        return "Take a third at each TP; after TP1 move the stop to entry."
    p = [round(x * 100) for x in w]
    return f"Take {p[0]}% at TP1, {p[1]}% at TP2, {p[2]}% at TP3; after TP1 move the stop to entry."


def caption(s):
    """Signal message built from the stored signal (used as the photo caption, max 1024 chars)."""
    icon = "🟢" if s["side"] == "LONG" else "🔴"
    nm = f"{s['coin']} on {s.get('chain')}" if s["market"] == "DEX" else f"{s['coin']}/USDT"
    trade_on = s.get("trade_on") or ("Spot" if s["market"] == "SPOT" else f"Perps {s.get('lev', 1)}x" if s["market"] == "PERP" else "On-chain spot")
    t = (f"{icon} {s['side']} {nm}\nTrade on: {trade_on}" + (f" ({s['market_reason']})" if s.get("market_reason") else "") +
         f"\n\nEntry {fmt(s['entry'])}\nStop loss {fmt(s['sl0'])} ({abs(s['sl0'] / s['entry'] - 1) * 100:.1f}%)\n"
         f"TP1 {fmt(s['tps'][0])}\nTP2 {fmt(s['tps'][1])}\nTP3 {fmt(s['tps'][2])} ({abs(s['tps'][2] / s['entry'] - 1) * 100:.1f}%)")
    if s["market"] == "PERP" and s.get("funding_pct") is not None:
        t += f"\nFunding {s['funding_pct']:+.4f}%"
    rs = [r for r in (s.get("reasons") or []) if not r.startswith("Untested")]
    tail = "\n\n" + plan_text(s) + "\nNot financial advice · " + C.SITE_URL
    if s["market"] == "DEX" and s.get("token"):
        tail = f"\n\nToken: {s['token']}\nUntested signal type. Check the address; copies exist." + tail
    why = ""
    for r in rs:
        add = ("\n\nWhy:" if not why else "") + "\n• " + r
        if len(t) + len(why) + len(add) + len(tail) > 1020:
            break
        why += add
    return t + why + tail


# ---------- formatting ----------
def fmt(p):
    if p >= 1000: return f"{p:.1f}"
    if p >= 100: return f"{p:.2f}"
    if p >= 10: return f"{p:.3f}"
    if p >= 1: return f"{p:.4f}"
    return f"{p:.5g}"


def signals():
    return jload(C.SIGNALS_JSON, [])


KIND = {"TA": "Chart breakout", "UNLOCK_SHORT": "Unlock short", "POST_UNLOCK_LONG": "Post-unlock long",
        "FUND_LONG": "Fundamentals long", "ONCHAIN": "On-chain spot", "HOT": "24h hot mover"}
MKT = {"PERP": "PERPS", "SPOT": "SPOT", "DEX": "ON-CHAIN SPOT"}


def open_text():
    op = [s for s in signals() if not s["closed"]]
    if not op:
        return "No open signals right now. New ones are checked every 15 minutes.\n\n" + C.SITE_URL
    out = ["Open signals:"]
    for s in op:
        lev = f" {s['lev']}x" if s["market"] == "PERP" else ""
        to = s.get("trade_on")
        st = "open" if not s["hits"] else f"TP{s['hits']} hit, stop at entry"
        mv = f"{s.get('move_pct', 0):+.1f}%" if s.get("move_pct") is not None else ""
        nm = f"{s['coin']} on {s['chain']}" if s["market"] == "DEX" else s["coin"]
        out.append(f"\n{nm} {MKT.get(s['market'], s['market'])} {s['side']}{lev} ({st}) {mv}\n"
                   f"Entry {fmt(s['entry'])}  SL {fmt(s['entry'] if s['hits'] else s['sl0'])}\n"
                   f"TP1 {fmt(s['tps'][0])}  TP2 {fmt(s['tps'][1])}  TP3 {fmt(s['tps'][2])}")
        if to:
            out.append(f"Trade on: {to}" + (f" ({s['market_reason']})" if s.get("market_reason") else ""))
        rs = [r for r in (s.get("reasons") or []) if not r.startswith("Chart:")][:3]
        if rs:
            out.append("Why: " + "; ".join(rs))
        if s["market"] == "DEX" and s.get("token"):
            out.append(f"Token: {s['token']}")
    out.append("\n" + C.SITE_URL)
    return "\n".join(out)


def cat(name):
    try:
        return json.load(open(os.path.join(C.DIR, "catalyst", name + ".json")))
    except (OSError, ValueError):
        return {}


def money(x):
    return f"${x / 1e9:.1f}B" if x >= 1e9 else f"${x / 1e6:.1f}M" if x >= 1e6 else f"${x / 1e3:.0f}k"


def unlocks_text():
    cal = cat("unlocks").get("calendar", [])
    now = time.time()
    rows = [u for u in cal if now < u["ts"] <= now + 14 * 86400 and (u["big"] or u["pct"] >= 0.5)]
    if not rows:
        return "No big token unlocks in the next 14 days for the coins we track.\n\n" + C.SITE_URL
    out = ["Token unlocks, next 14 days (big = 2%+ of supply or $10M+):"]
    for u in rows[:20]:
        d = datetime.fromtimestamp(u["ts"], timezone.utc).strftime("%b %d")
        who = (" · " + u["who"][0]) if u.get("who") else ""
        tag = "⚠️ " if u["big"] else ""
        trk = "" if u.get("tracked") else " (not in signal list)"
        out.append(f"{tag}{d}  {u['coin']}  {u['pct']:.1f}% of supply, {money(u['usd'])}{who}{trk}")
    out.append("\nBefore a big unlock: no new longs are favoured; after it passes and price holds, longs are allowed. "
               "Data: DefiLlama.\n\n" + C.SITE_URL)
    return "\n".join(out)


def watchlist_text():
    oc = cat("onchain").get("watchlist", [])
    sec = cat("sectors")
    out = []
    if oc:
        out.append("On-chain watchlist (trending, liquidity > $250k, 7+ days old, safety-checked):")
        for w in oc[:8]:
            st = "breakout now" if w.get("trigger") else "uptrend" if w.get("trend_up") else "watching"
            out.append(f"{w['symbol']} on {w['chain']}  {w['chg24h']:+.0f}% 24h, liq {money(w['liq'])} ({st})\n  {w['token']}")
    else:
        out.append("On-chain watchlist is empty right now.")
    rk = sec.get("ranked", [])
    if rk:
        names, ss = sec.get("names", {}), sec.get("sectors", {})
        out.append("\nLeading sectors (7d): " + ", ".join(f"{names.get(k, k)} {ss[k]['chg7d']:+.1f}%" for k in rk[:4]))
    out.append("\nOn-chain tokens are riskier than big coins. Check the contract address yourself.\n\n" + C.SITE_URL)
    return "\n".join(out)


SHIPPED_ROW = "A1: 25/25/50, stop->entry after TP1"
BASE_ROW = "baseline: thirds, stop->entry after TP1"


def stats_text():
    cl = [s for s in signals() if s["closed"]]
    bt = jload(C.BACKTEST_JSON, {}).get("summary", {}).get("all")
    lines = []
    if cl:
        w = sum(1 for s in cl if s["result_r"] > 0)
        lines.append(f"Live: {len(cl)} closed signals, {w} wins ({w / len(cl) * 100:.0f}%), "
                     f"total {sum(s['result_r'] for s in cl):+.1f}R")
        for m in C.MARKETS:
            x = [s for s in cl if s["market"] == m]
            if x:
                wx = sum(1 for s in x if s["result_r"] > 0)
                lines.append(f"  {m}: {len(x)} signals, {wx / len(x) * 100:.0f}% wins, {sum(s['result_r'] for s in x):+.1f}R")
    else:
        lines.append("Live: no closed signals yet.")
    lines.append(f"Open now: {sum(1 for s in signals() if not s['closed'])}")
    lines.append(f"\nStrategy {C.STRATEGY_VERSION}: {C.STRATEGY_NOTE}.")
    be = jload(os.path.join(C.DIR, "backtest_exits.json"), {}).get("rows", {})
    cur, old = be.get(SHIPPED_ROW, {}).get("all"), be.get(BASE_ROW, {}).get("all")
    if cur and old:
        lines.append(f"Backtest (past 90 days, ~200 coins, after fees, not live): {cur['trades']} signals, {cur['win_rate']}% wins, "
                     f"{cur['avg_r_net']:+.2f}R per signal (old exits {old['avg_r_net']:+.2f}R). "
                     "News filter is live-only (can't be backtested).")
    elif bt:
        lines.append(f"\nBacktest (past 90 days, not live): {bt['trades']} signals, {bt['win_rate']}% wins, "
                     f"{bt['avg_r']:+.2f}R per signal before fees, {bt['avg_r_net']:+.2f}R after fees.")
    lines.append("\n" + C.SITE_URL)
    return "\n".join(lines)


def welcome():
    return ("Welcome! You're subscribed to free crypto Spot + Perps signals on up to 350 of the most traded "
            "coins on OKX, plus a short list of 24h hot movers and on-chain spot picks.\n\nSignals use the chart "
            "plus catalysts: token unlocks, open interest and funding, TVL, fees and DEX volume, and sector strength. "
            "Hot movers are separate: a coin up hard in 24 hours with a volume spike can signal even if it is not "
            "in the chart list yet. Each signal says why in plain words, with an entry, stop loss and 3 take-profits. "
            "Perps signals suggest at most 3x leverage. Every result is tracked on the website:\n" + C.SITE_URL +
            "\n\n" + DISCLAIMER + "\n\nNobody from this bot will ever ask for your keys, seed phrase or money.\n\n" + help_text())


def help_text():
    return ("Commands:\n/signals  open signals and why\n/unlocks  upcoming token unlocks\n/watchlist  on-chain "
            "watchlist and leading sectors\n/stats  win rate and results\n/stop  stop receiving signals\n"
            "/start  subscribe again\n/help  this list")


def pretty(line):
    """Turn a run.py line into a Telegram message."""
    head, _, why = line.partition(" | ")
    kind = "TA"
    if " [" in head:
        head, _, k = head.partition(" [")
        kind = k.rstrip("]")
    w = head.split()
    if w[0] == "SIGNAL":
        market, side, coin = w[1], w[2], w[3]
        kv = dict(zip(w[4::2], w[5::2]))
        icon = "🟢" if side == "LONG" else "🔴"
        reasons = [r for r in why.split("; ") if r and not r.startswith("token ")] if why else []
        token = why.split(" | token ")[-1] if " | token " in line else None
        if token:
            reasons = [r for r in "; ".join(reasons).split(" | token ")[0].split("; ") if r]
        t = (f"{icon} NEW {MKT.get(market, market)} {side} {coin}\n{KIND.get(kind, kind)}\n"
             f"Entry {kv.get('entry')}\nStop loss {kv.get('SL')}\n"
             f"TP1 {kv.get('TP1')}\nTP2 {kv.get('TP2')}\nTP3 {kv.get('TP3')}")
        if market == "PERP":
            t += f"\nLeverage max {kv.get('lev')}"
            if "funding" in kv:
                t += f" · funding {kv['funding']}"
        if reasons:
            t += "\n\nWhy:\n" + "\n".join("• " + r for r in reasons[:7])
        if token:
            t += f"\n\nToken address: {token}\nCheck the address before buying; copies with the same name exist."
        t += "\n\n" + plan_text()
    elif w[0] == "NEWS":
        return "📰 News" + line[4:] + f"\n\nNot financial advice · {C.SITE_URL}"
    else:
        t = "📊 " + line.replace("UPDATE ", "", 1)
    return t + f"\n\nNot financial advice · {C.SITE_URL}"


# ---------- subscribers ----------
def is_live(rec):
    return not (isinstance(rec, dict) and rec.get("stopped"))


def live_ids(subs):
    return [k for k, v in subs.items() if is_live(v)]


def subscriber_count():
    return len(live_ids(jload(SUBS, {})))


def prune_subs(subs, now=None):
    """Drop stop-tombstones after a day. The timestamp blocks a stale join from subscribing them again."""
    now = now_ms() if now is None else now
    out = {}
    for k, v in subs.items():
        if isinstance(v, dict) and v.get("stopped") and now - int(v.get("ts") or 0) > TOMBSTONE_TTL:
            continue
        out[k] = v
    return out


def apply_event(subs, ev):
    """Apply one worker join/leave. Ignore it when a newer event for that chat is already stored."""
    chat = str(ev.get("chat") or "")
    if not chat or ev.get("op") not in ("start", "stop"):
        return False
    ts = int(ev.get("ts") or 0)
    cur = subs.get(chat)
    cur_ts = int(cur.get("ts") or 0) if isinstance(cur, dict) else 0
    if ts < cur_ts:
        return False
    if ev["op"] == "start":
        joined = ev.get("joined")
        subs[chat] = {"joined": int(joined if joined else time.time()),
                      "type": ev.get("type") or "private", "ts": ts}
    else:
        subs[chat] = {"stopped": True, "ts": ts}
    return True


def reply_bundle():
    return {"signals": open_text(), "stats": stats_text(), "unlocks": unlocks_text(),
            "watchlist": watchlist_text(), "help": help_text(), "welcome": welcome(), "stop": STOP_TEXT}


def write_replies():
    """Publish the command texts the worker sends. Skips the write when the words did not change."""
    bundle = reply_bundle()
    prev = jload(REPLIES, {})
    if all(prev.get(k) == bundle[k] for k in REPLY_KEYS):
        return bundle
    os.makedirs(os.path.dirname(REPLIES), exist_ok=True)
    tmp = REPLIES + ".tmp"
    json.dump({k: bundle[k] for k in REPLY_KEYS}, open(tmp, "w"), indent=1, ensure_ascii=False)
    os.replace(tmp, REPLIES)
    return bundle


def replies_fp(bundle):
    body = json.dumps({k: bundle[k] for k in REPLY_KEYS}, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(body).hexdigest()[:20]


# ---------- worker sync (no getUpdates) ----------
def webhook_env():
    """Return (base_url, secret) or (None, None). Never prints the secret."""
    url = (os.environ.get("SIGNALS_WEBHOOK_URL") or "").strip().rstrip("/")
    if url.endswith("/hook"):
        url = url[:-5].rstrip("/")
    secret = (os.environ.get("SIGNALS_WEBHOOK_SECRET") or "").strip()
    if not url and not secret:
        return None, None
    local = url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:")
    if not url or not secret or not (url.startswith("https://") or local) or not SECRET_RE.fullmatch(secret):
        print("bot: webhook skipped; need https SIGNALS_WEBHOOK_URL and a SIGNALS_WEBHOOK_SECRET "
              "of 1-256 chars A-Z a-z 0-9 _ -")
        return None, None
    return url, secret


def worker_healthy(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url.rstrip("/") + "/health"), timeout=15) as r:
            return 200 <= r.status < 300
    except (OSError, urllib.error.HTTPError):
        return False


def admin_json(method, path, payload=None, timeout=30):
    """Call the worker admin API. Prints the status only, never the secret or the body."""
    url, secret = webhook_env()
    if not url:
        return None
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url + path, data=body, method=method,
        headers={"Authorization": "Bearer " + secret, "Accept": "application/json",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        print(f"bot: worker {method} {path} failed ({e.code})")
        return None
    except OSError:
        print(f"bot: worker {method} {path} unreachable")
        return None
    try:
        return json.loads(raw.decode() or "null")
    except ValueError:
        print(f"bot: worker {method} {path} bad json")
        return None


def ack_events(keys):
    done = []
    for i in range(0, len(keys), ACK_BATCH):
        chunk = keys[i:i + ACK_BATCH]
        resp = admin_json("POST", "/admin/events/ack", {"keys": chunk})
        if not isinstance(resp, dict):
            break
        deleted = [k for k in (resp.get("deleted") or []) if k in chunk]
        done.extend(deleted)
        if len(deleted) < len(chunk):
            break
    return done


def sync_events():
    """Pull /start and /stop events into the encrypted subscriber list. Worker stays the command path."""
    st = jload(STATE, {})
    pending = list(st.get("ack_events") or [])
    if pending:
        done = set(ack_events(pending))
        pending = [k for k in pending if k not in done]
        st["ack_events"] = pending
        jsave(STATE, st)
    data = admin_json("GET", "/admin/events")
    if not isinstance(data, dict):
        print("bot: join/leave sync failed; keeping saved subscribers")
        return 0
    subs = prune_subs(jload(SUBS, {}))
    applied = 0
    keys = []
    events = list(data.get("events") or [])
    events.sort(key=lambda e: e.get("key") or "")
    for ev in events:
        key = ev.get("key")
        if not key:
            continue
        keys.append(key)
        if apply_event(subs, ev):
            applied += 1
    jsave(SUBS, subs)
    st = jload(STATE, {})
    st["ack_events"] = list(dict.fromkeys(list(st.get("ack_events") or []) + keys))
    jsave(STATE, st)
    if applied:
        print(f"bot: applied {applied} join/leave event(s)")
    return applied


def push_replies(bundle):
    fp = replies_fp(bundle)
    st = jload(STATE, {})
    if st.get("replies_fp") == fp:
        return True
    resp = admin_json("POST", "/admin/replies", {k: bundle[k] for k in REPLY_KEYS})
    if not isinstance(resp, dict) or not resp.get("ok"):
        print("bot: reply upload failed; worker will use the last copy or the site file")
        return False
    st = jload(STATE, {})
    st["replies_fp"] = fp
    jsave(STATE, st)
    print("bot: command replies updated")
    return True


def webhook_info():
    try:
        return api("getWebhookInfo")
    except TGError as e:
        print(f"bot: webhook check failed ({e.code})")
        return None


def ensure_webhook(url, secret):
    want = url + "/hook"
    info = api("getWebhookInfo")
    fp = hashlib.sha256(secret.encode()).hexdigest()[:16]
    st = jload(STATE, {})
    if info.get("url") == want and st.get("webhook_fp") == fp:
        err = (info.get("last_error_message") or "")[:180]
        if err:
            print("bot: webhook delivery error: " + err)
        return True
    api_json("setWebhook", {"url": want, "secret_token": secret, "allowed_updates": ["message"],
                            "drop_pending_updates": False, "max_connections": 8})
    st = jload(STATE, {})
    st["webhook_fp"] = fp
    jsave(STATE, st)
    print("bot: webhook set, polling off")
    return True


def webhook_off():
    api_json("deleteWebhook", {"drop_pending_updates": False})
    st = jload(STATE, {})
    st.pop("webhook_fp", None)
    jsave(STATE, st)
    print("bot: webhook removed; updates kept for the next poll")


def serve_plan(configured, healthy, webhook_url):
    """webhook: commands on the worker, this process broadcasts.
    broadcast-only: a webhook is already set, so getUpdates would 409 and drop nothing but also answer nothing.
    poll: no webhook yet."""
    if configured and healthy:
        return "webhook"
    if webhook_url:
        return "broadcast-only"
    return "poll"


# ---------- broadcast ----------
def flush():
    """Send unsent outbox lines to the channel (if set) and every live subscriber. Safe to call concurrently."""
    lock = open(os.path.join(C.PRIVATE_DIR, "flush.lock"), "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    subs = prune_subs(jload(SUBS, {}))
    if subs != jload(SUBS, {}):
        jsave(SUBS, subs)
    st = jload(STATE, {})
    if not os.path.exists(C.OUTBOX):
        return 0
    size = os.path.getsize(C.OUTBOX)
    pos = st.get("outbox_pos")
    if pos is None or pos > size:
        st["outbox_pos"] = size                            # first run: don't replay history
        jsave(STATE, st)
        return 0
    with open(C.OUTBOX) as fh:
        fh.seek(pos)
        chunk = fh.read()
    entries = [json.loads(l) for l in chunk.splitlines() if l.strip()]
    lines = [e["text"] for e in entries]
    if not lines:
        return 0
    subs = prune_subs(jload(SUBS, {}))
    chan = jload(CFG, {}).get("channel")
    targets = ([chan] if chan else []) + live_ids(subs)
    by_id = {x["id"]: x for x in signals()}
    for ent in entries:
        sig = by_id.get(ent.get("sig")) if ent.get("sig") else None
        msg = caption(sig) if sig else pretty(ent["text"])
        photo = ent.get("photo")
        photo = os.path.join(C.DIR, photo) if photo and os.path.exists(os.path.join(C.DIR, photo)) else None
        file_id = None
        for chat in targets:
            try:
                if photo:
                    try:
                        r = send_photo(chat, file_id or photo, msg[:1024], uploaded=bool(file_id))
                        file_id = file_id or (r.get("photo") or [{}])[-1].get("file_id")
                        if len(msg) > 1024:
                            send(chat, msg[1024:].lstrip() or "")
                    except TGError as e:
                        if e.code in (403,):
                            raise
                        log(f"photo failed ({e.code}), sending text")
                        send(chat, msg)
                else:
                    send(chat, msg)
            except TGError as e:
                if e.code in (403, 400) and chat in subs and is_live(subs.get(chat)):  # blocked the bot / chat gone
                    subs[chat] = {"stopped": True, "ts": now_ms()}
                    log(f"removed subscriber after {e.code}")
                elif e.code == 429:
                    time.sleep(5)
                else:
                    log(f"send failed: {e}")
            time.sleep(0.05)
    jsave(SUBS, subs)
    st = jload(STATE, {})
    newpos = pos + len(chunk.encode())
    if os.path.getsize(C.OUTBOX) == newpos:              # everything sent: start the outbox over
        open(C.OUTBOX, "w").close(); newpos = 0
    st["outbox_pos"] = newpos
    jsave(STATE, st)
    log(f"broadcast {len(lines)} line(s) to {len(targets)} chat(s)")
    return len(lines)


# ---------- commands ----------
def handle(msg):
    chat = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    subs = jload(SUBS, {})
    if cmd == "/start":
        cur = subs.get(chat)
        if not (isinstance(cur, dict) and cur.get("joined") and not cur.get("stopped")):
            subs[chat] = {"joined": int(time.time()), "type": msg["chat"].get("type"), "ts": now_ms()}
            jsave(SUBS, subs)
            log(f"new subscriber ({len(live_ids(subs))} total)")
        send(chat, welcome())
    elif cmd == "/stop":
        cur = subs.get(chat)
        if cur is not None and not (isinstance(cur, dict) and cur.get("stopped")):
            subs[chat] = {"stopped": True, "ts": now_ms()}
            jsave(SUBS, subs)
        send(chat, STOP_TEXT)
    elif cmd == "/signals":
        send(chat, open_text())
    elif cmd == "/stats":
        send(chat, stats_text())
    elif cmd == "/unlocks":
        send(chat, unlocks_text())
    elif cmd in ("/watchlist", "/catalysts"):
        send(chat, watchlist_text())
    elif cmd == "/help" or msg["chat"].get("type") == "private":
        send(chat, help_text())


def loop():
    info = webhook_info()
    if info and info.get("url"):
        sys.exit("bot: a webhook is set, so this loop would not receive updates. "
                 "Leave commands to the worker, or run: python3 bot.py --webhook-off")
    me = api("getMe")
    log(f"bot loop started as @{me['username']}")
    st = jload(STATE, {})
    last_flush = 0
    while True:
        try:
            ups = api("getUpdates", {"offset": st.get("offset", 0), "timeout": 25,
                                     "allowed_updates": json.dumps(["message"])}, timeout=40)
            for u in ups:
                st = jload(STATE, {}); st["offset"] = u["update_id"] + 1; jsave(STATE, st)
                m = u.get("message")
                if m and m.get("text"):
                    try:
                        handle(m)
                    except TGError as e:
                        log(f"reply failed: {e}")
            if time.time() - last_flush > 20:
                flush(); last_flush = time.time()
        except TGError as e:
            log(f"poll error: {e}")
            time.sleep(15 if e.code == 409 else 5)
        except Exception as e:                            # keep running; never log secrets
            log(f"loop error: {type(e).__name__}")
            time.sleep(5)


CMDS = [("signals", "Open signals and why"), ("unlocks", "Upcoming token unlocks"),
        ("watchlist", "On-chain watchlist and leading sectors"), ("stats", "Win rate and results"),
        ("help", "All commands"), ("stop", "Stop receiving signals")]
CMDS_V = 2


def menu():
    st = jload(STATE, {})
    if st.get("cmds_v") == CMDS_V:
        return
    try:
        api("setMyCommands", {"commands": json.dumps([{"command": c, "description": d} for c, d in CMDS])})
        st = jload(STATE, {}); st["cmds_v"] = CMDS_V; jsave(STATE, st)
        print("bot: command menu updated")
    except TGError as e:
        print(f"bot: command menu update failed ({e.code})")


def poll_once():
    """The only Actions path that calls getUpdates. A 409 means a webhook owns delivery; do not retry."""
    api("getMe")
    menu()
    try:
        ups = api("getUpdates", {"offset": jload(STATE, {}).get("offset", 0), "timeout": 0,
                                 "allowed_updates": json.dumps(["message"])}, timeout=30)
    except TGError as e:
        if e.code != 409:
            raise
        print("bot: getUpdates refused because a webhook is active; not polling")
        ups = []
    replied = 0
    for u in ups:
        st = jload(STATE, {}); st["offset"] = u["update_id"] + 1; jsave(STATE, st)
        m = u.get("message")
        if m and m.get("text"):
            try:
                handle(m); replied += 1
            except TGError as e:
                log(f"reply failed: {e}")
                print(f"bot: reply failed ({e.code})")
    n = flush()
    print(f"bot: ok, {len(ups)} update(s), {replied} handled, {n} broadcast line(s), "
          f"{subscriber_count()} subscriber(s)")


def broadcast_only(note):
    menu()
    n = flush()
    print(f"bot: {note}, {n} broadcast line(s), {subscriber_count()} subscriber(s)")


def once():
    """Single polling pass. Prints counts only, never chat ids or the token."""
    write_replies()
    info = webhook_info()
    if info and info.get("url"):
        broadcast_only("webhook active, not polling")
        return
    poll_once()


def serve():
    """Actions entry: webhook commands when the worker is up, otherwise today's polling pass."""
    bundle = write_replies()
    url, secret = webhook_env()
    info = webhook_info()
    current = (info or {}).get("url") or ""
    plan = serve_plan(bool(url), worker_healthy(url) if url else False, current)
    if plan == "poll":
        if url:
            print("bot: worker not reachable; polling this run")
        poll_once()
        return
    if plan == "broadcast-only":
        broadcast_only("webhook active, not polling")
        return
    try:
        ensure_webhook(url, secret)
    except TGError as e:
        print(f"bot: setWebhook failed ({e.code})")
        if not current:
            poll_once()
            return
    push_replies(bundle)
    sync_events()
    broadcast_only("webhook, not polling")


def main():
    os.makedirs(C.PRIVATE_DIR, exist_ok=True)
    if "--webhook-off" in sys.argv:
        webhook_off()
        if "--push" not in sys.argv:
            return
    if "--serve" in sys.argv:
        serve()
    elif "--once" in sys.argv:
        once()
    elif "--push" in sys.argv:
        n = flush()
        if n:
            print(f"bot: sent {n} line(s) to {subscriber_count()} subscriber(s)")
    elif "--status" in sys.argv:
        me = api("getMe")
        st = jload(STATE, {})
        size = os.path.getsize(C.OUTBOX) if os.path.exists(C.OUTBOX) else 0
        print(f"bot @{me['username']}, {subscriber_count()} subscriber(s), "
              f"{max(0, size - st.get('outbox_pos', size))} unsent bytes")
    else:
        lock = open(os.path.join(C.PRIVATE_DIR, "bot.lock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            sys.exit("bot: loop already running")
        loop()


if __name__ == "__main__":
    main()
