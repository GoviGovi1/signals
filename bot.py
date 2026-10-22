"""Telegram bot for the signal service (@Mhosignalbot).
Token: env SIGNALS_TG_TOKEN, else ALTSPOT_TELEGRAM_BOT_TOKEN. Never printed or written to disk.
  python3 bot.py           long-polling loop: answers /start /signals /stats /help /stop and broadcasts
                           every new SIGNAL/UPDATE line from run.py (private/outbox.jsonl) to subscribers
  python3 bot.py --once    one pass for GitHub Actions: getUpdates (stored offset), answer commands,
                           broadcast unsent outbox lines, then exit
  python3 bot.py --push    send any unsent outbox lines once, then exit (use if the loop is not running)
  python3 bot.py --status  show bot username, subscriber count, unsent lines
Optional: private/telegram.json {"channel": "@yourchannel"} also posts broadcasts to a channel
(the bot must be an admin there). Subscribers live in private/subscribers.json (never published)."""
import fcntl, json, os, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone
import config as C

SUBS = os.path.join(C.PRIVATE_DIR, "subscribers.json")
STATE = os.path.join(C.PRIVATE_DIR, "bot_state.json")
CFG = os.path.join(C.PRIVATE_DIR, "telegram.json")
LOG = os.path.join(C.PRIVATE_DIR, "bot.log")
DISCLAIMER = ("Not financial advice. These are paper-tracked signals: nothing is traded for you. "
              "You can lose money, and leverage makes losses bigger and faster.")


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


def api(method, params=None, timeout=20):
    url = f"https://api.telegram.org/bot{token()}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout)
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


def jload(path, default):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return default


def jsave(path, obj):
    tmp = path + ".tmp"
    json.dump(obj, open(tmp, "w"), indent=1)
    os.replace(tmp, path)


def send(chat, text):
    return api("sendMessage", {"chat_id": chat, "text": text, "disable_web_page_preview": "true"})


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
    tail = "\n\nTake a third at each TP; after TP1 move the stop to entry.\nNot financial advice · " + C.SITE_URL
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
        "FUND_LONG": "Fundamentals long", "ONCHAIN": "On-chain spot"}
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
    if bt:
        lines.append(f"\nBacktest (past 90 days, not live): {bt['trades']} signals, {bt['win_rate']}% wins, "
                     f"{bt['avg_r']:+.2f}R per signal before fees, {bt['avg_r_net']:+.2f}R after fees.")
    lines.append("\n" + C.SITE_URL)
    return "\n".join(lines)


def welcome():
    return ("Welcome! You're subscribed to free crypto Spot + Perps signals on about 200 of the most traded "
            "coins on OKX, plus on-chain spot picks.\n\nSignals use the chart plus catalysts: token unlocks, open "
            "interest and funding, TVL, fees and DEX volume, and sector strength. Each signal says why in plain "
            "words, with an entry, stop loss and 3 take-profits. Perps signals suggest at most 3x leverage. "
            "Every result is tracked on the website:\n" + C.SITE_URL +
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
        t += "\n\nTake a third at each TP, move the stop to entry after TP1."
    else:
        t = "📊 " + line.replace("UPDATE ", "", 1)
    return t + f"\n\nNot financial advice · {C.SITE_URL}"


# ---------- broadcast ----------
def flush():
    """Send unsent outbox lines to the channel (if set) and every subscriber. Safe to call concurrently."""
    lock = open(os.path.join(C.PRIVATE_DIR, "flush.lock"), "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
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
    subs = jload(SUBS, {})
    chan = jload(CFG, {}).get("channel")
    targets = ([chan] if chan else []) + list(subs)
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
                if e.code in (403, 400) and chat in subs:     # blocked the bot / chat gone
                    subs.pop(chat, None); log(f"removed subscriber after {e.code}")
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
        if chat not in subs:
            subs[chat] = {"joined": int(time.time()), "type": msg["chat"].get("type")}
            jsave(SUBS, subs)
            log(f"new subscriber ({len(subs)} total)")
        send(chat, welcome())
    elif cmd == "/stop":
        if subs.pop(chat, None) is not None:
            jsave(SUBS, subs)
        send(chat, "You won't get signals anymore. Send /start to subscribe again.")
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


def once():
    """Single non-blocking pass (GitHub Actions every ~15 min). Prints counts only, never chat ids or the token."""
    api("getMe")
    st = jload(STATE, {})
    if st.get("cmds_v") != CMDS_V:
        try:
            api("setMyCommands", {"commands": json.dumps([{"command": c, "description": d} for c, d in CMDS])})
            st["cmds_v"] = CMDS_V; jsave(STATE, st)
            print("bot: command menu updated")
        except TGError as e:
            print(f"bot: command menu update failed ({e.code})")
    ups = api("getUpdates", {"offset": st.get("offset", 0), "timeout": 0,
                             "allowed_updates": json.dumps(["message"])}, timeout=30)
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
          f"{len(jload(SUBS, {}))} subscriber(s)")


def main():
    os.makedirs(C.PRIVATE_DIR, exist_ok=True)
    if "--once" in sys.argv:
        once()
    elif "--push" in sys.argv:
        n = flush()
        if n:
            print(f"bot: sent {n} line(s) to {len(jload(SUBS, {}))} subscriber(s)")
    elif "--status" in sys.argv:
        me = api("getMe")
        st = jload(STATE, {})
        size = os.path.getsize(C.OUTBOX) if os.path.exists(C.OUTBOX) else 0
        print(f"bot @{me['username']}, {len(jload(SUBS, {}))} subscriber(s), "
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
