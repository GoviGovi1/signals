"""Check for new signals and update open ones. Prints one line per NEW signal or status change, else nothing.
Signals only: reads public OKX candles, never touches a wallet or exchange account.
usage: python3 run.py"""
import csv, fcntl, json, os, sys, time
from datetime import datetime, timezone
import config as C, okx, strategy as S

H = S.H
FIELDS = ["id", "coin", "market", "side", "opened_iso", "entry", "sl0", "tp1", "tp2", "tp3", "lev", "funding_pct",
          "status", "hits", "closed_iso", "exit_price", "result_r", "result_pct"]


def fmt(p):
    if p >= 1000: return f"{p:.1f}"
    if p >= 100: return f"{p:.2f}"
    if p >= 10: return f"{p:.3f}"
    if p >= 1: return f"{p:.4f}"
    return f"{p:.5g}"


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def load():
    if os.path.exists(C.SIGNALS_JSON):
        return json.load(open(C.SIGNALS_JSON))
    return []


def save(sigs):
    tmp = C.SIGNALS_JSON + ".tmp"
    json.dump(sigs, open(tmp, "w"), indent=1)
    os.replace(tmp, C.SIGNALS_JSON)
    tmp = C.SIGNALS_CSV + ".tmp"
    with open(tmp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for s in sigs:
            row = dict(s, tp1=s["tps"][0], tp2=s["tps"][1], tp3=s["tps"][2], opened_iso=iso(s["opened"]),
                       closed_iso=iso(s["closed_at"]) if s.get("closed_at") else "")
            w.writerow(row)
    os.replace(tmp, C.SIGNALS_CSV)


def lev_txt(s, pct):
    if s["market"] != "PERP" or pct is None:
        return ""
    return f" ({pct * s['lev']:+.1f}% at {s['lev']}x)"


def signal_line(s):
    t = (f"SIGNAL {s['market']} {s['side']} {s['coin']} entry {fmt(s['entry'])} SL {fmt(s['sl0'])} "
         f"TP1 {fmt(s['tps'][0])} TP2 {fmt(s['tps'][1])} TP3 {fmt(s['tps'][2])}")
    if s["market"] == "PERP":
        t += f" lev {s['lev']}x"
        if s.get("funding_pct") is not None:
            t += f" funding {s['funding_pct']:+.4f}%"
    return t


def update_lines(s, events):
    head = f"UPDATE {s['coin']} {s['market']} {s['side']}"
    out = []
    for e in events:
        if e.startswith("tp"):
            k = int(e[2])
            mv = k * s["R"] / s["entry"] * 100
            extra = " (stop moved to entry)" if k == 1 and not s["closed"] else ""
            if k == 3:
                extra = f", closed total {s['result_pct']:+.1f}%{lev_txt(s, s['result_pct'])}"
            out.append(f"{head} TP{k} hit {mv:+.1f}%{lev_txt(s, mv)}{extra}")
        elif e == "stopped":
            out.append(f"{head} stopped {s['result_pct']:+.1f}%{lev_txt(s, s['result_pct'])}")
        elif e == "stopped_be":
            out.append(f"{head} stopped at entry, closed total {s['result_pct']:+.1f}%{lev_txt(s, s['result_pct'])}")
        elif e == "expired":
            out.append(f"{head} expired after {C.P['expire_h']}h, closed total {s['result_pct']:+.1f}%{lev_txt(s, s['result_pct'])}")
    return out


def finish(s, now_ms):
    if s["closed"] and not s.get("closed_at"):
        s["closed_at"] = now_ms
        s["result_pct"] = round(S.realized_pct(s), 2)
        s["result_r"] = round(s["result_r"], 3)


def main():
    lock = open(os.path.join(C.PRIVATE_DIR, "run.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    sigs = load()
    lines, now_ms = [], int(time.time() * 1000)
    errors = []
    for coin in C.COINS:
        for m in C.MARKETS:
            try:
                b1 = okx.recent(coin, m, "1h", 300)
                b4 = okx.recent(coin, m, "4h", 300, confirmed_only=True)
            except Exception as e:                      # one bad market must not stop the others
                errors.append(f"{coin} {m}: {type(e).__name__}")
                continue
            done = [b for b in b1 if b["done"]]
            last = b1[-1]["c"]
            # 1) update open signals
            for s in sigs:
                if s["closed"] or s["coin"] != coin or s["market"] != m:
                    continue
                ev, prev = [], None
                for b in b1:
                    if b["ts"] < s["next_bar"]:
                        prev = b["c"]
                        continue
                    if b["ts"] + H > s["opened"] + C.P["expire_h"] * H and b["done"] and prev is not None:
                        S.expire(s, prev); ev.append("expired")      # 48h window ended at the previous candle
                        break
                    ev += S.step(s, b)
                    prev = b["c"]
                    if b["done"]:
                        s["next_bar"] = b["ts"] + H
                    if s["closed"]:
                        break
                if not s["closed"] and now_ms >= s["opened"] + C.P["expire_h"] * H:
                    S.expire(s, last); ev.append("expired")
                finish(s, now_ms)
                s["last"] = last
                s["last_ts"] = now_ms
                if not s["closed"]: s["move_pct"] = round((last - s["entry"]) / s["entry"] * 100 * (1 if s["side"] == "LONG" else -1), 2)
                if m == "PERP" and not s["closed"]:
                    try:
                        s["funding_pct"] = round(okx.funding(coin) * 100, 4)
                    except Exception:
                        pass
                lines += update_lines(s, ev)
            # 2) new signal on the last closed 1h candle
            if len(done) < 60 or any(not s["closed"] and s["coin"] == coin and s["market"] == m for s in sigs):
                continue
            i = len(done) - 1
            bar_ts = done[i]["ts"]
            if any(s["coin"] == coin and s["market"] == m and s["bar_ts"] == bar_ts for s in sigs):
                continue
            ind = S.indicators(done, b4)
            side = S.signal_at(done, ind, i, m)
            if not side:
                continue
            t = S.new_trade(side, done[i]["c"], ind["atr"][i], bar_ts + H)
            s = dict(t, id=f"{coin}-{m}-{bar_ts // 1000}", coin=coin, market=m, bar_ts=bar_ts,
                     side="LONG" if side == 1 else "SHORT", next_bar=bar_ts + H, last=last, last_ts=now_ms,
                     rsi=round(ind["rsi"][i], 1), atr=ind["atr"][i], result_pct=None, closed_at=None,
                     move_pct=round((last - t["entry"]) / t["entry"] * 100 * side, 2))
            if m == "PERP":
                try:
                    s["funding_pct"] = round(okx.funding(coin) * 100, 4)
                except Exception:
                    s["funding_pct"] = None
            else:
                s["lev"] = 1
            sigs.append(s)
            lines.append(signal_line(s))
            # the forming candle may already have moved through a level
            ev = S.step(s, b1[-1]) if not b1[-1]["done"] else []
            finish(s, now_ms)
            lines += update_lines(s, ev)
    save(sigs)
    if len(errors) == len(C.COINS) * len(C.MARKETS):
        state = os.path.join(C.PRIVATE_DIR, "outage")
        if not os.path.exists(state):
            open(state, "w").write(iso(now_ms))
            lines.append("ERROR market data unavailable (OKX); will keep retrying")
    elif os.path.exists(os.path.join(C.PRIVATE_DIR, "outage")):
        os.remove(os.path.join(C.PRIVATE_DIR, "outage"))
        lines.append("OK market data is back")
    if lines:
        with open(C.OUTBOX, "a") as fh:
            for ln in lines:
                if ln.startswith(("SIGNAL", "UPDATE")):
                    fh.write(json.dumps({"ts": now_ms, "text": ln}) + "\n")
        print("\n".join(lines))


if __name__ == "__main__":
    main()
