"""Check for new signals and update open ones. Prints one line per NEW signal or status change, else nothing.
Signals only: reads public market data (OKX, DefiLlama, CoinGecko, GeckoTerminal, GoPlus), never touches a
wallet or exchange account.

Signal kinds
  TA                chart breakout (strategy.py) on ~200 liquid OKX coins, with catalyst reasons attached and
                    catalyst filters applied where the backtest showed they help (config.CATALYST_FILTERS)
  UNLOCK_SHORT      perps short before a big token unlock, with a bearish chart          (if switched on)
  POST_UNLOCK_LONG  spot long after a big unlock passed and price held                   (if switched on)
  FUND_LONG         spot long when TVL / fees / DEX volume jump while price lags          (if switched on)
  ONCHAIN           on-chain (DEX) spot watchlist breakout; untested: no free history to backtest
usage: python3 run.py [--no-refresh]"""
import csv, fcntl, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import config as C, chartread as CR, okx, strategy as S, catalyst as K, universe, news as N

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


# ---------------- market choice ----------------
def lev_for(stop_frac, cap):
    """Leverage so a stop costs ~8% of margin AND liquidation (~1/L minus ~1% maintenance) sits at least 3x
    further away than the stop. Capped."""
    if stop_frac <= 0:
        return 1
    return max(1, min(cap, int(C.P["lev_target_loss"] / stop_frac), int(1 / (3 * stop_frac + 0.01))))


def choose_market(coin, side, stop_frac, atrp, strong, der):
    """('SPOT'|'PERP', leverage, reason in plain words) or (None, 0, why not)."""
    inf = universe.info(coin)
    fund8 = (der or {}).get("funding_8h_pct")
    thin = inf.get("vol_perp", 0) < 10e6
    wild = atrp > 3.0
    cap = 1 if (thin or wild) else C.P["max_lev"]
    if side < 0:
        if not inf.get("perp"):
            return None, 0, "no perpetual to short"
        L = lev_for(stop_frac, cap)
        why = "shorts need perps"
        if wild: why += "; very volatile, so 1x"
        elif thin: why += "; thin perp market, so 1x"
        else: why += f"; {L}x keeps liquidation 3x+ further away than the stop"
        return "PERP", L, why
    if not inf.get("perp"):
        return "SPOT", 1, "only spot is listed"
    if not inf.get("spot"):
        L = lev_for(stop_frac, cap)
        return "PERP", L, f"only perps are listed; {L}x keeps liquidation far from the stop"
    L = lev_for(stop_frac, cap)
    cheap = fund8 is not None and fund8 <= 0.01
    if strong and cheap and not wild and not thin and L >= 2:
        return "PERP", L, (f"strong trend (coin and BTC up, beating BTC) and cheap funding ({fund8:+.3f}%/8h); "
                           f"{L}x keeps liquidation 3x+ further away than the stop")
    if wild:
        why = "very volatile coin: spot avoids liquidation"
    elif thin:
        why = "thin perp market: spot is safer"
    elif fund8 is not None and not cheap:
        why = f"funding {fund8:+.3f}%/8h is not cheap: spot avoids paying it"
    elif not strong:
        why = "trend not strong enough for leverage: spot has no liquidation or funding"
    else:
        why = "spot: no liquidation or funding"
    return "SPOT", 1, why


# ---------------- main ----------------
def fetch(key):
    coin, m = key
    try:
        return key, okx.recent(coin, m, "1h", 300), okx.recent(coin, m, "4h", 300, confirmed_only=True), None
    except Exception as e:
        return key, None, None, type(e).__name__


def fetch1(key, limit=100):
    coin, m = key
    try:
        return key, okx.recent(coin, m, "1h", limit), None
    except Exception as e:
        return key, None, type(e).__name__


SCAN = os.path.join(C.DIR, "catalyst", "scan_state.json")


def v3_filters(side, rs, vr, btc_trend, atrp=None):
    """Strategy v3 extra rules (switched on in config.V3_FILTERS by backtest200.py). Returns (ok, reasons)."""
    F, ok, rs_ = C.V3_FILTERS, True, []
    if btc_trend is not None:
        rs_.append("BTC 4h trend " + {1: "up", -1: "down", 0: "flat"}[btc_trend])
        if F.get("BTC regime") and btc_trend == -side:
            ok = False
    if rs is not None:
        rs_.append(f"{'Stronger' if rs > 0 else 'Weaker'} than BTC by {abs(rs):.1f}% over 7d")
        if F.get("relative strength vs BTC") and side * rs <= 0:
            ok = False
    if vr is not None:
        rs_.append(f"Breakout volume {vr:.1f}x the 20h average")
        if F.get("volume 1.5x") and vr < 1.5:
            ok = False
    if atrp is not None and F.get("ATR% < 2.5 (calmer coins)") and atrp >= 2.5:
        rs_.append(f"Too volatile (ATR {atrp:.1f}%/h)"); ok = False
    if F.get("longs only") and side == -1:
        rs_.append("shorts switched off"); ok = False
    return ok, rs_


def prune_charts(sigs):
    d = os.path.join(C.SITE_DIR, "charts")
    if not os.path.isdir(d):
        return
    closed = sorted((s for s in sigs if s["closed"]), key=lambda s: s.get("closed_at") or 0, reverse=True)
    keep = {os.path.basename(s["chart"]) for s in sigs if s.get("chart") and (not s["closed"] or s in closed[:40])}
    for f in os.listdir(d):
        if f.endswith(".png") and f not in keep:
            os.remove(os.path.join(d, f))
    for s in closed[40:]:
        s.pop("chart", None)


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
    try:
        nr = N.refresh()
        if nr:
            print(f"news: {nr[0]} headlines fetched, {nr[1]} coin-matched kept" + (f" | failed: {'; '.join(nr[2])}" if nr[2] else ""))
    except Exception as e:
        print(f"news: refresh failed ({type(e).__name__})")
    sigs = load()
    lines, outbox, now_ms = [], [], int(time.time() * 1000)
    # 1) track open exchange signals on their own market (one small call each)
    open_keys = sorted({(s["coin"], s["market"]) for s in sigs if not s["closed"] and s["market"] != "DEX"})
    with ThreadPoolExecutor(3) as ex:
        got_open = {k: (b, e) for k, b, e in ex.map(fetch1, open_keys)}
    errors = [f"{k}: {e}" for k, (b, e) in got_open.items() if e]
    for s in sigs:
        if s["closed"] or s["market"] == "DEX":
            continue
        b1 = got_open.get((s["coin"], s["market"]), (None, None))[0]
        if not b1:
            continue
        ev = track(s, b1, now_ms)
        if s["market"] == "PERP" and not s["closed"]:
            try:
                s["funding_pct"] = round(okx.funding(s["coin"]) * 100, 4)
            except Exception:
                pass
        lines += update_lines(s, ev)
    if C.NEWS.get("followups"):
        try:
            lines += N.followups(sigs, now_ms)
        except Exception as e:
            print(f"news: follow-ups failed ({type(e).__name__})")
    # 2) scan the universe once per closed hour (the first run after the hour does it)
    st = K.jload("scan_state")
    last_closed = (now_ms // H) * H - H
    scanned = 0
    if st.get("bar_ts") != last_closed or "--scan" in sys.argv:
        coins = universe.coins()
        keys = [(c, universe.info(c).get("series") or "PERP") for c in coins]
        with ThreadPoolExecutor(4) as ex:
            got = list(ex.map(fetch, keys))
        scanned = len(keys)
        errs = [f"{c} {m}: {e}" for (c, m), _, _, e in got if e]
        errors += errs
        if len(errs) < len(keys) // 2:
            K.jsave("scan_state", {"bar_ts": last_closed, "updated": int(time.time()), "coins": len(keys), "errors": len(errs)})
        lines += scan(sigs, got, now_ms)
    # 3) on-chain (DEX) watchlist signals
    lines += dex_signals(sigs, now_ms)
    prune_charts(sigs)
    save(sigs)
    if scanned and len(errors) >= scanned:
        state = os.path.join(C.PRIVATE_DIR, "outage")
        if not os.path.exists(state):
            open(state, "w").write(iso(now_ms))
            lines.append("ERROR market data unavailable (OKX); will keep retrying")
    elif scanned and os.path.exists(os.path.join(C.PRIVATE_DIR, "outage")):
        os.remove(os.path.join(C.PRIVATE_DIR, "outage"))
        lines.append("OK market data is back")
    if lines:
        with open(C.OUTBOX, "a") as fh:
            for ln in lines:
                if isinstance(ln, dict):
                    fh.write(json.dumps(dict(ln, ts=now_ms)) + "\n")
                elif ln.startswith(("SIGNAL", "UPDATE")):
                    fh.write(json.dumps({"ts": now_ms, "text": ln}) + "\n")
        print("\n".join(ln["text"] if isinstance(ln, dict) else ln for ln in lines))
    print(f"run: {len(open_keys)} open tracked, {scanned} coins scanned, {len(errors)} fetch errors, {time.time() - t0:.0f}s")


def scan(sigs, got, now_ms):
    lines = []
    cx = Ctx()
    data = {c: (m, b1, b4) for (c, m), b1, b4, e in got if not e and b1 and b4}
    btc = data.get("BTC")
    btc_trend, btc_7d = None, None
    if btc:
        tr = S.trend_series(btc[2])
        btc_trend = tr[-1][1] if tr else None
        bd = [b for b in btc[1] if b["done"]]
        btc_7d = bd[-1]["c"] / bd[-169]["c"] - 1 if len(bd) > 169 else None
    n_open = sum(1 for s in sigs if not s["closed"] and s["market"] != "DEX")
    today = sum(1 for s in sigs if s["market"] != "DEX" and s["opened"] // (86400 * 1000) == now_ms // (86400 * 1000))
    gate = K.coin_gate() if C.COIN_GATE else None
    cands = []
    for coin, (m, b1, b4) in data.items():
        done = [b for b in b1 if b["done"]]
        if len(done) < 200:
            continue
        if any(not s["closed"] and s["coin"] == coin and s["market"] != "DEX" for s in sigs):
            continue
        i = len(done) - 1
        bar_ts = done[i]["ts"]
        if any(s["coin"] == coin and s.get("bar_ts") == bar_ts for s in sigs):
            continue
        inf = universe.info(coin)
        sides = (1, -1) if inf.get("perp") else (1,)
        P = CR.prep(done, b4)
        for k in range(max(200, i - 26), i):             # replay recent bars so retests know their breakout
            CR.triggers(P, k, sides)
        V4 = C.V4
        for setup, side, info in CR.triggers(P, i, sides):
            if setup not in V4["setups"]:
                continue
            rd = CR.read(P, i, side, setup, info)
            f = rd["features"]
            if f["room"] < V4["min_room_r"]:
                print(f"filtered {coin} {setup}: next level only {f['room']:.1f}R away"); continue
            if f["atrp"] >= V4["max_atr_pct"]:
                print(f"filtered {coin} {setup}: too volatile (ATR {f['atrp']:.1f}%/h)"); continue
            rs = (done[i]["c"] / done[i - 168]["c"] - 1 - btc_7d) * 100 if btc_7d is not None and i >= 168 else None
            vr = f["vr"]
            cands.append((side * (rs or 0), coin, m, b1, b4, done, P["ind"], i, side, rs, vr, setup, rd))
            break                                        # one setup per coin (order: range, retest, BOS, 20h)
    cands.sort(key=lambda x: -x[0])                     # strongest relative strength first
    for _, coin, m, b1, b4, done, ind, i, side, rs, vr, setup, rd in cands:
        if n_open >= C.MAX_OPEN or today >= C.MAX_NEW_PER_DAY:
            print(f"cap reached, skipped {coin} ({n_open} open, {today} today)")
            continue
        ok, v3r = v3_filters(side, rs, None, btc_trend, None)
        if not ok:
            print(f"filtered {coin} {'LONG' if side > 0 else 'SHORT'}: v3 rules ({'; '.join(v3r)})")
            continue
        inf = universe.info(coin)
        der = cx.derivs(coin) if inf.get("perp") else {}
        c = cx.coin(coin, b1, b4)
        score, veto, crs = assess(coin, side, m, c, der)
        if gate and coin not in gate.get("net_r", {}):
            veto = veto or f"no 45-day track record for {coin} yet"
        elif gate and gate["net_r"][coin] <= 0:
            veto = veto or f"strategy lost {gate['net_r'][coin]:+.1f}R on {coin} in the last 45 days"
        elif gate:
            crs.append(f"Track record: {gate['net_r'][coin]:+.1f}R on {coin} in the last 45 days (backtest)")
        nveto, nline = N.assess(coin, side)
        if nveto and C.NEWS.get("veto"):
            veto = veto or nveto
        if veto:
            print(f"filtered {coin} {'LONG' if side > 0 else 'SHORT'}: {veto}")
            continue
        atr_v, entry = ind["atr"][i], done[i]["c"]
        atrp = atr_v / entry * 100
        f = rd["features"]
        strong = side > 0 and f["t4"] == 1 and f["d1"] == 1 and btc_trend == 1 and (rs or 0) > 0
        market, lev, mwhy = choose_market(coin, side, rd["R"] / entry, atrp, strong, der)
        scale = 1.0
        if not market:
            continue
        bars, b4m = b1, b4
        if market != m:                                  # levels from the market you'll actually trade
            try:
                bars = okx.recent(coin, market, "1h", 300)
                b4m = okx.recent(coin, market, "4h", 300, confirmed_only=True)
                d2 = [b for b in bars if b["done"]]
                k2 = next(k for k, b in enumerate(d2) if b["ts"] == done[i]["ts"])
                ind2 = S.indicators(d2, b4m)
                scale = d2[k2]["c"] / entry                 # same chart levels, priced on that market
                entry, atr_v = d2[k2]["c"], ind2["atr"][k2]
            except Exception:
                bars, market, lev = b1, m, (lev if m == "PERP" else 1)
                mwhy += " (fallback: other market's data unavailable)"
        bar_ts, last = done[i]["ts"], bars[-1]["c"]
        t = S.new_trade(side, entry, atr_v, bar_ts + H)
        sl = rd["sl"] * scale
        t.update(sl=sl, sl0=sl, R=abs(entry - sl), tps=[x * scale for x in rd["tps"]])
        trade_on = "Spot" if market == "SPOT" else f"Perps {lev}x"
        s = dict(t, id=f"{coin}-{market}-{bar_ts // 1000}", kind="TA", coin=coin, market=market, bar_ts=bar_ts,
                 side="LONG" if side == 1 else "SHORT", next_bar=bar_ts + H, last=last, last_ts=now_ms,
                 rsi=round(ind["rsi"][i], 1), atr=atr_v, result_pct=None, closed_at=None,
                 move_pct=round((last - entry) / entry * 100 * side, 2), score=score, lev=lev,
                 trade_on=trade_on, market_reason=mwhy,
                 setup=setup, reasons=[f"Chart: {x}" if k == 0 else x for k, x in enumerate(rd["reasons"])][:1] + ([nline] if nline else [])
                 + rd["reasons"][1:] + v3r + crs, w=list(C.V4["exit_weights"]), strategy=C.STRATEGY_VERSION,
                 levels=[round(x * scale, 10) for x in rd["plot"]["levels"]],
                 range_box=[rd["plot"]["range"][0] * scale, rd["plot"]["range"][1] * scale, rd["plot"]["range"][2]] if rd["plot"].get("range") else None)
        s["funding_pct"] = der.get("funding_pct") if market == "PERP" else None
        try:
            import chart
            path = os.path.join(C.SITE_DIR, "charts", f"{s['id']}.png")
            chart.render(s, bars, path)
            s["chart"] = f"charts/{s['id']}.png"
        except Exception as e:
            print(f"chart failed for {coin}: {type(e).__name__}")
        sigs.append(s)
        n_open += 1; today += 1
        line = signal_line(s)
        lines.append({"text": line, "sig": s["id"], "photo": os.path.join("site", s["chart"]) if s.get("chart") else None})
        ev = S.step(s, bars[-1]) if not bars[-1]["done"] else []
        finish(s, now_ms)
        lines += update_lines(s, ev)
    return lines


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
                 score=None, reasons=rs, lev=1, trade_on=f"On-chain spot ({w['chain']} DEX)",
                 market_reason="not listed on OKX; buy on a DEX, small size, no leverage")
        try:
            import chart
            os.makedirs(os.path.join(C.SITE_DIR, "charts"), exist_ok=True)
            chart.render(s, K.dex_bars(w["net"], w["pool"], 100), os.path.join(C.SITE_DIR, "charts", f"{sid}.png"))
            s["chart"] = f"charts/{sid}.png"
        except Exception as e:
            print(f"dex chart failed for {w['symbol']}: {type(e).__name__}")
        sigs.append(s)
        open_dex.append(s); today.append(s)
        lines.append({"text": signal_line(s) + f" | token {w['token']}", "sig": sid,
                      "photo": os.path.join("site", s["chart"]) if s.get("chart") else None})
    return lines


if __name__ == "__main__":
    main()
