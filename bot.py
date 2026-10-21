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


# ---------- formatting ----------
def fmt(p):
    if p >= 1000: return f"{p:.1f}"
    if p >= 100: return f"{p:.2f}"
    if p >= 10: return f"{p:.3f}"
    if p >= 1: return f"{p:.4f}"
    return f"{p:.5g}"


def signals():
    return jload(C.SIGNALS_JSON, [])


def open_text():
    op = [s for s in signals() if not s["closed"]]
    if not op:
        return "No open signals right now. New ones are checked every 15 minutes.\n\n" + C.SITE_URL
    out = ["Open signals:"]
    for s in op:
        lev = f" {s['lev']}x" if s["market"] == "PERP" else ""
        st = "open" if not s["hits"] else f"TP{s['hits']} hit, stop at entry"
        mv = f"{s.get('move_pct', 0):+.1f}%" if s.get("move_pct") is not None else ""
        out.append(f"\n{s['coin']} {s['market']} {s['side']}{lev} ({st}) {mv}\n"
                   f"Entry {fmt(s['entry'])}  SL {fmt(s['entry'] if s['hits'] else s['sl0'])}\n"
                   f"TP1 {fmt(s['tps'][0])}  TP2 {fmt(s['tps'][1])}  TP3 {fmt(s['tps'][2])}")
    out.append("\n" + C.SITE_URL)
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
    return ("Welcome! You're subscribed to free crypto Spot + Perps signals for BTC, ETH, SOL, BNB, XRP, "
            "DOGE, SUI, AVAX and LINK.\n\nEach signal has an entry, stop loss and 3 take-profits. Perps "
            "signals suggest at most 3x leverage. Every result is tracked on the website:\n" + C.SITE_URL +
            "\n\n" + DISCLAIMER + "\n\nNobody from this bot will ever ask for your keys, seed phrase or money.\n\n" + help_text())


def help_text():
    return ("Commands:\n/signals  open signals\n/stats  win rate and results\n/stop  stop receiving signals\n"
            "/start  subscribe again\n/help  this list")


def pretty(line):
    """Turn a run.py line into a Telegram message."""
    w = line.split()
    if w[0] == "SIGNAL":
        market, side, coin = w[1], w[2], w[3]
        kv = dict(zip(w[4::2], w[5::2]))
        icon = "🟢" if side == "LONG" else "🔴"
        t = (f"{icon} NEW {('PERPS' if market == 'PERP' else 'SPOT')} {side} {coin}\n"
             f"Entry {kv.get('entry')}\nStop loss {kv.get('SL')}\n"
             f"TP1 {kv.get('TP1')}\nTP2 {kv.get('TP2')}\nTP3 {kv.get('TP3')}")
        if market == "PERP":
            t += f"\nLeverage max {kv.get('lev')}"
            if "funding" in kv:
                t += f" · funding {kv['funding']}"
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
    lines = [json.loads(l)["text"] for l in chunk.splitlines() if l.strip()]
    if not lines:
        return 0
    subs = jload(SUBS, {})
    chan = jload(CFG, {}).get("channel")
    targets = ([chan] if chan else []) + list(subs)
    for ln in lines:
        msg = pretty(ln)
        for chat in targets:
            try:
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


def once():
    """Single non-blocking pass (GitHub Actions every ~15 min). Prints counts only, never chat ids or the token."""
    api("getMe")
    st = jload(STATE, {})
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
