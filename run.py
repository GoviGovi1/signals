"""Check for new signals and update open ones. Prints one line per NEW signal or status change, else nothing.
Signals only: reads public market data (OKX, DefiLlama, CoinGecko, GeckoTerminal, GoPlus), never touches a
wallet or exchange account.

Signal kinds
  TA                chart breakout (strategy.py) on ~40 liquid OKX coins, with catalyst reasons attached and
                    catalyst filters applied where the backtest showed they help (config.CATALYST_FILTERS)
  UNLOCK_SHORT      perps short before a big token unlock, with a bearish chart          (if switched on)
  POST_UNLOCK_LONG  spot long after a big unlock passed and price held                   (if switched on)
  FUND_LONG         spot long when TVL / fees / DEX volume jump while price lags          (if switched on)
  ONCHAIN           on-chain (DEX) spot watchlist breakout; untested: no free history to backtest
usage: python3 run.py [--no-refresh]"""
import csv, fcntl, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import config as C, okx, strategy as S, catalyst as K, universe

H = S.H
DAY = 86400
FIELDS = ["id", "kind", "coin", "market", "side", "opened_iso", "entry", "sl0", "tp1", "tp2", "tp3", "lev", "funding_pct",
          "status", "hits", "closed_iso", "exit_price", "result_r", "result_pct", "score", "reasons", "chain", "token"]
KIND_LABEL = {"TA": "Chart breakout", "UNLOCK_SHORT": "Unlock short", "POST_UNLOCK_LONG": "Post-unlock long",
              "FUND_LONG": "Fundamentals long", "ONCHAIN": "On-chain spot"}


def fmt(p):
    if p >= 1000: return f"{p:.1f}"
    if p >= 100: return f"{p:.2f}"
    if p >= 10: return f"{p:.3f}"
    if p >= 1: return f"{p:.4f}"
    return f"{p:.5g}"


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def money(x):
    return f"${x / 1e9:.1f}B" if x >= 1e9 else f"${x / 1e6:.1f}M" if x >= 1e6 else f"${x / 1e3:.0f}k"


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
                       closed_iso=iso(s["closed_at"]) if s.get("closed_at") else "", kind=s.get("kind", "TA"),
                       reasons=" | ".join(s.get("reasons") or []))
            w.writerow(row)
    os.replace(tmp, C.SIGNALS_CSV)


def lev_txt(s, pct):
    if s["market"] != "PERP" or pct is None:
        return ""
    return f" ({pct * s['lev']:+.1f}% at {s['lev']}x)"


def name(s):
    return f"{s['coin']} ({s['chain']})" if s["market"] == "DEX" else s["coin"]


def signal_line(s):
    t = (f"SIGNAL {s['market']} {s['side']} {s['coin']} entry {fmt(s['entry'])} SL {fmt(s['sl0'])} "
         f"TP1 {fmt(s['tps'][0])} TP2 {fmt(s['tps'][1])} TP3 {fmt(s['tps'][2])}")
    if s["market"] == "PERP":
        t += f" lev {s['lev']}x"
        if s.get("funding_pct") is not None:
            t += f" funding {s['funding_pct']:+.4f}%"
    t += f" [{s.get('kind', 'TA')}]"
    if s.get("reasons"):
        t += " | " + "; ".join(s["reasons"])
    return t


def update_lines(s, events):
    head = f"UPDATE {name(s)} {s['market']} {s['side']}"
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


def track(s, b1, now_ms):
    """Walk an open signal through new candles. Returns events."""
    ev, prev = [], None
    last = b1[-1]["c"]
    for b in b1:
        if b["ts"] < s["next_bar"]:
            prev = b["c"]
            continue
        if b["ts"] + H > s["opened"] + C.P["expire_h"] * H and b["done"] and prev is not None:
            S.expire(s, prev); ev.append("expired")
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
    if not s["closed"]:
        s["move_pct"] = round((last - s["entry"]) / s["entry"] * 100 * (1 if s["side"] == "LONG" else -1), 2)
    return ev


# ---------------- catalyst assessment ----------------
class Ctx:
    """Per-run cache of catalyst facts per coin."""
    def __init__(self):
        self.d = {}
        self.der = {}

    def coin(self, coin, b1, b4):
        if coin not in self.d:
            c = b1[-1]["c"]
            p24 = (c / b1[-25]["c"] - 1) * 100 if len(b1) > 25 else None
            p7 = (c / b4[-43]["c"] - 1) * 100 if len(b4) > 43 else None
            self.d[coin] = {"unlock": K.unlock_view(coin), "fund": K.fund_view(coin, p7), "sector": K.sector_view(coin),
                            "p24": p24, "p7": p7}
        return self.d[coin]

    def derivs(self, coin):
        if coin not in self.der:
            self.der[coin] = K.derivs(coin)
        return self.der[coin]


def unlock_reasons(u, now):
    rs = []
    if u.get("first_ts") and u.get("next7_pct", 0) >= 0.25:
        days = max(0, (u["first_ts"] - now) / DAY)
        when = f"in {max(1, round(days * 24))} h" if days < 1 else f"in {days:.0f} day{'s' if round(days) != 1 else ''}"
        rs.append(f"Unlock: {u['next7_pct']:.1f}% of supply ({money(u['next7_usd'])}) {when}" + (" (big)" if u["big_next7"] else ""))
    if u.get("recent_big"):
        r = u["recent_big"]
        rs.append(f"Unlock of {r['pct']:.1f}% passed {(now - r['ts']) / DAY:.0f} days ago")
    return rs


def assess(coin, side, market, cx, der):
    """Catalyst score, veto and plain-word reasons for a chart signal."""
    now = time.time()
    F = C.CATALYST_FILTERS
    score, veto, rs = 0, None, []
    u = cx["unlock"]
    rs += unlock_reasons(u, now)
    if u.get("big_next7"):
        score += -2 if side > 0 else 1
        if side > 0 and F["unlock_long_veto"]:
            veto = "big unlock within 7 days"
    if u.get("recent_big") and side > 0:
        score += 1
    if der:
        conf, skips, drs = K.derivs_eval(side, der, cx["p24"])
        rs += drs
        score += 1 if conf else 0
        score -= 1 if skips else 0
        for k in skips:
            if F.get(k):
                veto = veto or {"oi_falling": "open interest falling", "exhaustion": "OI exhaustion",
                                "crowded": "crowded funding"}[k]
        if F["require_oi_confirm"] and not conf:
            veto = veto or "no OI confirmation"
    else:
        rs.append("OI / funding: no data this run")
    fv = cx["fund"]
    rs += fv["reasons"]
    if fv["momentum"]:
        score += 1 if side > 0 else -1
    sv = cx["sector"]
    if sv:
        rs.append(f"Sector: {sv['sector']} {sv['chg7d']:+.1f}% 7d ({sv['tier']}, #{sv['rank']} of {sv['of']})")
        if sv["tier"] == "leading":
            score += 1 if side > 0 else -1
        elif sv["tier"] == "lagging":
            score += -1 if side > 0 else 1
    return score, veto, rs


def catalyst_kind(coin, market, done, ind, cx):
    """Stand-alone catalyst signal on the last closed candle (same rules as backtest_catalyst.run_catalyst)."""
    i = len(done) - 1
    ef, es, r, a, tr4 = ind["ef"][i], ind["es"][i], ind["rsi"][i], ind["atr"][i], ind["trend"][i]
    if None in (ef, es, r, a):
        return None, 0, []
    c, t, now = done[i]["c"], done[i]["ts"] / 1000 + 3600, time.time()
    info = K.jload("unlocks").get("coins", {}).get(coin) or {}
    on = C.CATALYST_SIGNALS
    if market == "PERP" and on.get("UNLOCK_SHORT"):
        for u in info.get("upcoming", []):
            if t + DAY < u["ts"] <= t + 7 * DAY and K.is_big(u) and c < es and ef < es and 30 <= r <= 50 and tr4 != 1:
                return "UNLOCK_SHORT", -1, [f"Unlock: {u['pct']:.1f}% of supply ({money(u['usd'])}) in {(u['ts'] - now) / DAY:.0f} days",
                                            "Chart: below the 1h EMA50, EMA20 under EMA50"]
    if market == "SPOT" and on.get("POST_UNLOCK_LONG"):
        for u in info.get("recent", []):
            if t - 5 * DAY <= u["ts"] <= t - DAY and K.is_big(u):
                k0 = max(0, i - int((t - u["ts"]) // 3600))
                if c >= done[k0]["c"] and c > es and ef > es and 50 <= r <= 70 and tr4 != -1:
                    return "POST_UNLOCK_LONG", 1, [f"Unlock of {u['pct']:.1f}% passed {(now - u['ts']) / DAY:.0f} days ago and price held",
                                                   "Chart: above the 1h EMA50, EMA20 over EMA50"]
    if market == "SPOT" and on.get("FUND_LONG") and cx["fund"]["momentum"] and c > es and ef > es and 50 <= r <= 70 and tr4 != -1:
        return "FUND_LONG", 1, cx["fund"]["reasons"] + ["Chart: turning up above the 1h EMA50"]
    return None, 0, []


def ta_reason(side, r, tr4):
    if side > 0:
        return f"Chart: 20-hour breakout above EMA50, 4h uptrend, RSI {r:.0f}"
    return f"Chart: 20-hour breakdown below EMA50, 4h downtrend, RSI {r:.0f}"


# ---------------- main ----------------
def fetch(key):
    coin, m = key
    try:
        return key, okx.recent(coin, m, "1h", 300), okx.recent(coin, m, "4h", 300, confirmed_only=True), None
    except Exception as e:
        return key, None, None, type(e).__name__


def main():
    os.makedirs(C.PRIVATE_DIR, exist_ok=True)
    lock = open(os.path.join(C.PRIVATE_DIR, "run.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    t0 = time.time()
    if "--no-refresh" not in sys.argv:
        done_, failed = K.refresh()
        if done_ or failed:
            print("catalyst:", ", ".join(done_) or "-", ("| failed: " + "; ".join(failed)) if failed else "")
    sigs = load()
    coins = universe.coins()
    open_coins = [s["coin"] for s in sigs if not s["closed"] and s["market"] != "DEX"]
    coins = list(dict.fromkeys(coins + open_coins))
    lines, now_ms = [], int(time.time() * 1000)
    keys = [(c, m) for c in coins for m in C.MARKETS]
    with ThreadPoolExecutor(3) as ex:
        got = list(ex.map(fetch, keys))
    errors = [f"{c} {m}: {e}" for (c, m), _, _, e in got if e]
    cx = Ctx()
    n_open = sum(1 for s in sigs if not s["closed"] and s["market"] != "DEX")
    cands = []
    for (coin, m), b1, b4, err in got:
        if err or not b1:
            continue
        done = [b for b in b1 if b["done"]]
        # 1) update open signals
        for s in sigs:
            if s["closed"] or s["coin"] != coin or s["market"] != m:
                continue
            ev = track(s, b1, now_ms)
            if m == "PERP" and not s["closed"]:
                try:
                    s["funding_pct"] = round(okx.funding(coin) * 100, 4)
                except Exception:
                    pass
            lines += update_lines(s, ev)
        # 2) candidate on the last closed 1h candle (one open signal per coin per market)
        if coin not in universe.coins() or len(done) < 60:
            continue
        if any(not s["closed"] and s["coin"] == coin and s["market"] == m for s in sigs):
            continue
        i = len(done) - 1
        bar_ts = done[i]["ts"]
        if any(s["coin"] == coin and s["market"] == m and s["bar_ts"] == bar_ts for s in sigs):
            continue
        ind = S.indicators(done, b4)
        side = S.signal_at(done, ind, i, m)
        kind, reasons = "TA", []
        c = cx.coin(coin, b1, b4)
        if side:
            reasons = [ta_reason(side, ind["rsi"][i], ind["trend"][i])]
        else:
            kind, side, reasons = catalyst_kind(coin, m, done, ind, c)
            if not kind:
                continue
        cands.append((coin, m, b1, done, ind, i, side, kind, reasons, c))
    for coin, m, b1, done, ind, i, side, kind, reasons, c in cands:
        if n_open >= C.MAX_OPEN:
            print(f"skip {coin} {m}: {C.MAX_OPEN} signals already open")
            continue
        der = cx.derivs(coin)
        score, veto, crs = assess(coin, side, m, c, der)
        gate = K.coin_gate() if C.COIN_GATE and kind == "TA" else None
        if gate and coin not in gate.get("net_r", {}):
            veto = veto or f"no 45-day track record for {coin} yet"
        elif gate and gate["net_r"][coin] <= 0:
            veto = veto or f"strategy lost {gate['net_r'][coin]:+.1f}R on {coin} in the last 45 days"
        elif gate and coin in gate.get("net_r", {}):
            crs.append(f"Track record: {gate['net_r'][coin]:+.1f}R on {coin} in the last 45 days (backtest)")
        if veto and kind == "TA":
            print(f"filtered {coin} {m} {'LONG' if side > 0 else 'SHORT'}: {veto}")
            continue
        bar_ts, last = done[i]["ts"], b1[-1]["c"]
        t = S.new_trade(side, done[i]["c"], ind["atr"][i], bar_ts + H)
        s = dict(t, id=f"{coin}-{m}-{bar_ts // 1000}", kind=kind, coin=coin, market=m, bar_ts=bar_ts,
                 side="LONG" if side == 1 else "SHORT", next_bar=bar_ts + H, last=last, last_ts=now_ms,
                 rsi=round(ind["rsi"][i], 1), atr=ind["atr"][i], result_pct=None, closed_at=None,
                 move_pct=round((last - t["entry"]) / t["entry"] * 100 * side, 2),
                 score=score, reasons=reasons + [x for x in crs if x not in reasons])
        if m == "PERP":
            s["funding_pct"] = der.get("funding_pct")
        else:
            s["lev"] = 1
        sigs.append(s)
        n_open += 1
        lines.append(signal_line(s))
        ev = S.step(s, b1[-1]) if not b1[-1]["done"] else []
        finish(s, now_ms)
        lines += update_lines(s, ev)
    # 3) on-chain (DEX) watchlist signals
    lines += dex_signals(sigs, now_ms)
    save(sigs)
    if len(errors) == len(keys):
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
    print(f"run: {len(coins)} coins, {len(errors)} fetch errors, {time.time() - t0:.0f}s")


def dex_signals(sigs, now_ms):
    if not C.CATALYST_SIGNALS.get("ONCHAIN"):
        return []
    lines = []
    K.GT_DEADLINE[0] = time.time() + 25
    for s in sigs:                                     # track open ones on their own DEX pool candles
        if s["closed"] or s["market"] != "DEX":
            continue
        try:
            bars = K.dex_bars(s["net"], s["pool"], 72)
        except Exception:
            continue
        if bars:
            lines += update_lines(s, track(s, bars, now_ms))
    K.GT_DEADLINE[0] = 0.0
    oc = K.jload("onchain")
    open_dex = [s for s in sigs if not s["closed"] and s["market"] == "DEX"]
    today = [s for s in sigs if s["market"] == "DEX" and now_ms - s["opened"] < DAY * 1000]
    for w in oc.get("watchlist", []):
        if len(open_dex) >= C.MAX_OPEN_DEX or len(today) >= C.MAX_NEW_DEX_PER_DAY:
            break
        if not w.get("trigger") or now_ms - w["bar_ts"] > 3 * H:
            continue
        sid = f"{w['symbol']}-DEX-{w['net']}-{w['token'][:10]}-{w['bar_ts'] // 1000}"
        if any(x["id"] == sid or (x["market"] == "DEX" and x.get("token") == w["token"] and not x["closed"]) for x in sigs):
            continue
        t = S.new_trade(1, w["close"], w["atr"], w["bar_ts"] + H)
        rs = [f"Trending on {w['chain']} (GeckoTerminal), {money(w['vol24h'])} volume 24h",
              f"Liquidity {money(w['liq'])}, pool {w['age_d']:.0f} days old",
              "Safety: GoPlus OK" if w.get("safety") == "ok" else "Safety: GoPlus had no data, be careful",
              f"Chart: 20-hour breakout above EMA50 on the DEX, RSI {w['rsi']:.0f}",
              "Untested: no free history to backtest on-chain signals"]
        s = dict(t, id=sid, kind="ONCHAIN", coin=w["symbol"], market="DEX", chain=w["chain"], net=w["net"],
                 token=w["token"], pool=w["pool"], url=w["url"], bar_ts=w["bar_ts"], side="LONG",
                 next_bar=w["bar_ts"] + H, last=w["price"], last_ts=now_ms, rsi=w["rsi"], atr=w["atr"],
                 result_pct=None, closed_at=None, move_pct=round((w["price"] - t["entry"]) / t["entry"] * 100, 2),
                 score=None, reasons=rs, lev=1)
        sigs.append(s)
        open_dex.append(s); today.append(s)
        lines.append(signal_line(s) + f" | token {w['token']}")
    return lines


if __name__ == "__main__":
    main()
