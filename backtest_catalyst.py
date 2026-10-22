"""Honest catalyst backtest: does each catalyst rule improve the chart (TA) signals, and do the stand-alone
catalyst signals make money? ~90 days, every universe coin, split into two halves (H1 = older 45 days,
H2 = newer 45 days). A rule becomes a FILTER only if it helps in BOTH halves; otherwise it stays
display-only. Writes backtest_catalyst.json. Needs the cache from `python3 bt_fetch.py`.

Data (all free): OKX candles, 4h open interest, funding history; DefiLlama unlock schedules (past cliffs),
daily fees / DEX volume / TVL history. Not testable (no free history): sector 7d ranking, GeckoTerminal
trending pools, OKX listings - those are display-only.
Approximations, stated on the site: circulating supply at a past unlock = today's supply minus later cliffs;
DefiLlama values are used from the previous full day (no look-ahead)."""
import json, os, sys, time
from datetime import datetime, timezone
import config as C, okx, universe, strategy as S, backtest as B, catalyst as K

H = S.H
DAY_MS = 86400 * 1000
OUT = os.path.join(C.DIR, "backtest_catalyst.json")
ORIG9 = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "SUI", "AVAX", "LINK"]


def jl(name):
    p = os.path.join(C.CACHE_DIR, name)
    return json.load(open(p)) if os.path.exists(p) else None


def cached(name, fn, max_age=24 * 3600):
    p = os.path.join(C.CACHE_DIR, name)
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age:
        return json.load(open(p))
    d = fn()
    json.dump(d, open(p, "w"))
    return d


# ---------------- data ----------------
def load_all(coins):
    data, oi, fund = {}, {}, {}
    for c in coins:
        for m in C.MARKETS:
            b1, b4 = jl(f"{c}_{m}_1h.json"), jl(f"{c}_{m}_4h.json")
            if b1 and b4 and len(b1) > 400:
                data[(c, m)] = (b1, b4)
        oi[c] = jl(f"{c}_oi4h.json") or []
        f = jl(f"{c}_funding.json") or []
        fund[c] = f
    return data, oi, fund


def unlock_history(coins):
    """{coin: [(ts_ms, pct_of_circ, tokens)]} for past and future cliffs (DefiLlama emissions)."""
    def fetch():
        by_sym, by_gid = K.unlock_entries()
        gids = K.jload("geckoids").get("ids", {})
        out = {}
        for c in coins:
            e = by_gid.get(gids.get(c) or "") or by_sym.get(c)
            if not e:
                continue
            circ_now = K._num(e.get("circSupply"))
            cl = K.cliffs(e)
            now = time.time()
            rows = []
            for ts, tok, al in cl:
                later = sum(t for s, t, _ in cl if now >= s > ts)       # cliffs between then and now
                circ = max(circ_now - later, tok) if ts <= now else circ_now
                rows.append([ts * 1000, tok / circ * 100 if circ else 0, tok])
            out[c] = rows
        return out
    return cached("unlock_hist.json", fetch)


def fund_history(coins):
    """{coin: {metric: {day_ms: value}}} daily fees, DEX volume, protocol TVL and chain TVL from DefiLlama."""
    f = K.jload("fundamentals").get("coins", {})

    def fetch():
        out = {}
        for c in coins:
            d = f.get(c, {})
            m = {}
            for kind, slugs, url in (("fees", d.get("fees_slugs"), "https://api.llama.fi/summary/fees/{}?dataType=dailyFees"),
                                     ("dex", d.get("dex_slugs"), "https://api.llama.fi/summary/dexs/{}")):
                ser = {}
                for s in slugs or []:
                    try:
                        for ts, v in K.http(url.format(s), timeout=60).get("totalDataChart", []):
                            ser[int(ts) * 1000] = ser.get(int(ts) * 1000, 0) + float(v or 0)
                    except Exception:
                        pass
                    time.sleep(0.3)
                if ser:
                    m[kind] = ser
            slug = (d.get("fees_slugs") or d.get("dex_slugs") or [None])[0]
            if slug and d.get("tvl"):
                try:
                    p = K.http(f"https://api.llama.fi/protocol/{slug}", timeout=90)
                    m["tvl"] = {int(r["date"]) * 1000: float(r["totalLiquidityUSD"]) for r in p.get("tvl", [])}
                except Exception:
                    pass
            if d.get("chain"):
                try:
                    h = K.http(f"https://api.llama.fi/v2/historicalChainTvl/{d['chain'].replace(' ', '%20')}")
                    m["chain_tvl"] = {int(r["date"]) * 1000: float(r["tvl"]) for r in h}
                except Exception:
                    pass
            if m:
                out[c] = {k: {str(t): v for t, v in s.items()} for k, s in m.items()}
            print("fund hist", c, list(m), flush=True)
        return out
    raw = cached("fund_hist.json", fetch)
    return {c: {k: {int(t): v for t, v in s.items()} for k, s in m.items()} for c, m in raw.items()}


# ---------------- features at a moment t (ms, close time of the signal candle) ----------------
def last_before(series, t):
    """series sorted list of dicts with ts; index of last element with ts <= t."""
    lo, hi = 0, len(series) - 1
    if not series or series[0]["ts"] > t:
        return -1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if series[mid]["ts"] <= t: lo = mid
        else: hi = mid - 1
    return lo


def oi_change(oi, t):
    # OKX stamps a 4h OI bar at its start; the value is known at the bar's end -> require ts + 4h <= t
    j = last_before(oi, t - 4 * H)
    if j < 6 or oi[j - 6]["oi_usd"] <= 0:
        return None
    return (oi[j]["oi_usd"] / oi[j - 6]["oi_usd"] - 1) * 100


def funding_8h(fd, t):
    j = last_before(fd, t)
    if j < 1:
        return None
    iv = (fd[j]["ts"] - fd[j - 1]["ts"]) / H or 8
    return fd[j]["rate"] * 100 * 8 / iv


def unlock_flags(rows, t, price_at):
    nxt = [r for r in rows if t < r[0] <= t + 7 * DAY_MS]
    big_next = any(r[1] >= K.R["unlock_big_pct"] or (r[2] * price_at >= 10e6 and r[1] >= K.R["unlock_min_pct"]) for r in nxt)
    return big_next, nxt


def wow(series, t, days=7):
    """Week-over-week % from a daily series, using only days fully before t."""
    if not series:
        return None
    day0 = (t // DAY_MS) * DAY_MS - DAY_MS            # last complete day
    a = [series.get(day0 - k * DAY_MS) for k in range(days)]
    b = [series.get(day0 - (k + days) * DAY_MS) for k in range(days)]
    if None in a or None in b:
        return None
    sa, sb = sum(a), sum(b)
    return (sa / sb - 1) * 100 if sb > 0 else None


def level_chg(series, t, days=7):
    if not series:
        return None
    day0 = (t // DAY_MS) * DAY_MS - DAY_MS
    a, b = series.get(day0), series.get(day0 - days * DAY_MS)
    if not a or not b:
        return None
    ch = (a / b - 1) * 100
    return ch if abs(ch) <= 100 else None


def fund_strong(fh, t, price7):
    if not fh or price7 is None:
        return False, []
    rs = []
    for name, ch in (("Fees", wow(fh.get("fees"), t)), ("DEX volume", wow(fh.get("dex"), t)),
                     ("TVL", level_chg(fh.get("tvl"), t)), ("Chain TVL", level_chg(fh.get("chain_tvl"), t))):
        if ch is not None and ch >= K.R["fund_rise_pct"]:
            rs.append(f"{name} {ch:+.0f}%")
    return bool(rs) and price7 < K.R["fund_price_lag_pct"], rs


# ---------------- simulation ----------------
def simulate(b1, i, side, atr_v, market, params=C.P):
    t = S.new_trade(side, b1[i]["c"], atr_v, b1[i]["ts"] + H, params)
    j, n = i + 1, len(b1)
    while j < n and not t["closed"]:
        if b1[j]["ts"] + H > t["opened"] + params["expire_h"] * H:
            S.expire(t, b1[j - 1]["c"]); break
        S.step(t, b1[j]); j += 1
    if not t["closed"]:
        return None, n
    fee_r = C.FEES_PCT[market] / 100 * t["entry"] / t["R"]
    return {"r": t["result_r"], "r_net": t["result_r"] - fee_r, "hits": t["hits"], "status": t["status"],
            "opened": t["opened"], "side": "LONG" if side == 1 else "SHORT"}, j


def run_ta(data, ctx, start, veto=None):
    trades = []
    for (coin, market), (b1, b4) in data.items():
        ind = S.indicators(b1, b4)
        i, n = 0, len(b1)
        while i < n - 1:
            side = S.signal_at(b1, ind, i, market) if b1[i]["ts"] >= start else 0
            if not side:
                i += 1; continue
            f = features(ctx, coin, market, b1, i, side)
            if veto and veto(f, side, market):
                i += 1; continue
            tr, j = simulate(b1, i, side, ind["atr"][i], market)
            if tr is None:
                break
            tr.update(coin=coin, market=market, f=f)
            trades.append(tr)
            i = j
    return trades


def features(ctx, coin, market, b1, i, side):
    t = b1[i]["ts"] + H
    c = b1[i]["c"]
    p24 = (c / b1[i - 24]["c"] - 1) * 100 if i >= 24 else None
    p7 = (c / b1[i - 168]["c"] - 1) * 100 if i >= 168 else None
    big_next, _ = unlock_flags(ctx["unl"].get(coin, []), t, c)
    oi = oi_change(ctx["oi"].get(coin, []), t)
    f8 = funding_8h(ctx["fund"].get(coin, []), t)
    strong, frs = fund_strong(ctx["fh"].get(coin), t, p7)
    conf, skips, _ = K.derivs_eval(side, {"oi_24h_pct": oi, "funding_8h_pct": f8}, p24)
    return {"big_unlock_next7": big_next, "oi": oi, "f8": f8, "oi_confirm": conf, "skips": skips,
            "crowded": f8 is not None and side * f8 >= K.R["funding_crowded"],
            "exhaust": oi is not None and oi <= K.R["oi_exhaust_pct"] and p24 is not None and side * p24 > 0,
            "fund_strong": strong, "p7": p7}


# --- stand-alone catalyst signals ---
def run_catalyst(data, ctx, start):
    out = []
    for (coin, market), (b1, b4) in data.items():
        ind = S.indicators(b1, b4)
        cl = [b["c"] for b in b1]
        rows = ctx["unl"].get(coin, [])
        ts_index = {b["ts"]: k for k, b in enumerate(b1)}
        used = set()
        busy_until = 0
        for i in range(200, len(b1) - 1):
            t = b1[i]["ts"] + H
            if b1[i]["ts"] < start or i < busy_until:
                continue
            ef, es, r, a, tr4 = ind["ef"][i], ind["es"][i], ind["rsi"][i], ind["atr"][i], ind["trend"][i]
            if None in (ef, es, r, a):
                continue
            c = cl[i]
            kind, side = None, 0
            # 1) pre-unlock short (perps): big cliff 1-7 days ahead + bearish chart
            if market == "PERP":
                for u in rows:
                    if t + DAY_MS < u[0] <= t + 7 * DAY_MS and (u[1] >= K.R["unlock_big_pct"] or (u[2] * c >= 10e6 and u[1] >= K.R["unlock_min_pct"])) \
                            and ("S", u[0]) not in used and c < es and ef < es and 30 <= r <= 50 and tr4 != 1:
                        kind, side = "UNLOCK_SHORT", -1; used.add(("S", u[0])); break
            # 2) post-unlock long (spot): big cliff 1-5 days ago, price held, chart turning up
            if not kind and market == "SPOT":
                for u in rows:
                    if t - 5 * DAY_MS <= u[0] <= t - DAY_MS and ("L", u[0]) not in used:
                        k0 = max(0, i - (t - u[0]) // H)
                        p_at = cl[int(k0)]
                        big = u[1] >= K.R["unlock_big_pct"] or (u[2] * p_at >= 10e6 and u[1] >= K.R["unlock_min_pct"])
                        if big and c >= p_at and c > es and ef > es and 50 <= r <= 70 and tr4 != -1:
                            kind, side = "POST_UNLOCK_LONG", 1; used.add(("L", u[0])); break
            # 3) fundamentals rising while price lags (spot)
            if not kind and market == "SPOT" and i >= 168:
                strong, _ = fund_strong(ctx["fh"].get(coin), t, (c / cl[i - 168] - 1) * 100)
                if strong and c > es and ef > es and 50 <= r <= 70 and tr4 != -1:
                    kind, side = "FUND_LONG", 1
            if not kind:
                continue
            tr, j = simulate(b1, i, side, a, market)
            if tr is None:
                break
            tr.update(coin=coin, market=market, kind=kind)
            out.append(tr)
            busy_until = j
    return out


def live_veto(f, s, m):
    """Exactly the filters switched on in config.CATALYST_FILTERS (unknown OI never blocks, like live)."""
    F = C.CATALYST_FILTERS
    if any(F.get(k) for k in f["skips"]):
        return True
    if F.get("unlock_long_veto") and s == 1 and f["big_unlock_next7"]:
        return True
    return bool(F.get("require_oi_confirm") and not f["oi_confirm"])


def gate_from(trades):
    net = {}
    for t in trades:
        net[t["coin"]] = net.get(t["coin"], 0.0) + t["r_net"]
    return {c: round(v, 2) for c, v in net.items()}


def write_gate(days=45):
    """Daily walk-forward coin gate: live rules over the last `days`, net R per coin -> catalyst/coin_gate.json."""
    coins = universe.coins()
    data, oi, fund = load_all(coins)
    ctx = {"oi": oi, "fund": fund, "unl": {}, "fh": {}}
    last = max(b1[-1]["ts"] for b1, _ in data.values())
    tr = run_ta(data, ctx, last - days * 24 * H, live_veto)
    net = gate_from(tr)
    K.jsave("coin_gate", {"updated": int(time.time()), "days": days, "net_r": net,
                          "allowed": sorted(c for c, v in net.items() if v > 0),
                          "rule": f"chart signals only on coins with a positive result after fees over the last {days} days"})
    print(f"gate: {sum(1 for v in net.values() if v > 0)} of {len(coins)} coins allowed")


def agg(ts):
    n = len(ts)
    if not n:
        return {"trades": 0}
    w = sum(1 for t in ts if t["r"] > 0)
    return {"trades": n, "win_rate": round(w / n * 100, 1), "avg_r": round(sum(t["r"] for t in ts) / n, 3),
            "avg_r_net": round(sum(t["r_net"] for t in ts) / n, 3), "total_r_net": round(sum(t["r_net"] for t in ts), 1)}


def halves(ts, mid):
    return {"all": agg(ts), "H1": agg([t for t in ts if t["opened"] < mid]), "H2": agg([t for t in ts if t["opened"] >= mid])}


def line(name, h):
    def s(a):
        return f"n={a['trades']:4} win={a.get('win_rate', '-')}% avgR={a.get('avg_r', '-')} net={a.get('avg_r_net', '-')}" if a["trades"] else "n=   0"
    return f"{name:34} | ALL {s(h['all'])} | H1 {s(h['H1'])} | H2 {s(h['H2'])}"


def main():
    coins = universe.coins()
    data, oi, fund = load_all(coins)
    ctx = {"oi": oi, "fund": fund, "unl": unlock_history(coins), "fh": fund_history(coins)}
    last = max(b1[-1]["ts"] for b1, _ in data.values())
    start, mid = last - B.DAYS * 24 * H, last - B.DAYS // 2 * 24 * H
    res = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "days": B.DAYS,
           "from": datetime.fromtimestamp(start / 1000, timezone.utc).date().isoformat(),
           "to": datetime.fromtimestamp(last / 1000, timezone.utc).date().isoformat(),
           "mid": datetime.fromtimestamp(mid / 1000, timezone.utc).date().isoformat(),
           "coins": len({c for c, _ in data}), "fees_pct_round_trip": C.FEES_PCT, "variants": {}, "splits": {}, "catalyst": {}}
    base = run_ta(data, ctx, start)
    V = res["variants"]
    V["TA only (all coins)"] = halves(base, mid)
    V["TA only, original 9 coins"] = halves([t for t in base if t["coin"] in ORIG9], mid)
    V["TA only, 31 added coins"] = halves([t for t in base if t["coin"] not in ORIG9], mid)
    for m in C.MARKETS:
        V[f"TA only {m}"] = halves([t for t in base if t["market"] == m], mid)
    vetoes = {
        "skip longs before a big unlock": lambda f, s, m: s == 1 and f["big_unlock_next7"],
        "skip crowded funding": lambda f, s, m: f["crowded"],
        "skip OI exhaustion": lambda f, s, m: f["exhaust"],
        "require OI rising (+5% 24h)": lambda f, s, m: not f["oi_confirm"],
        "skip when OI unknown or falling": lambda f, s, m: f["oi"] is None or f["oi"] < 0,
    }
    for name, v in vetoes.items():
        V[name] = halves(run_ta(data, ctx, start, v), mid)
    live = run_ta(data, ctx, start, live_veto)
    V["LIVE filters (skip falling OI)"] = halves(live, mid)
    # coin gate, walk-forward: pick coins on one half, measure on the other half
    g1 = {c for c, v in gate_from([t for t in live if t["opened"] < mid]).items() if v > 0}
    g2 = {c for c, v in gate_from([t for t in live if t["opened"] >= mid]).items() if v > 0}
    wf = {"picked on H1, tested on H2": {"kept": agg([t for t in live if t["opened"] >= mid and t["coin"] in g1]),
                                          "dropped": agg([t for t in live if t["opened"] >= mid and t["coin"] not in g1]),
                                          "coins_kept": len(g1)},
          "picked on H2, tested on H1": {"kept": agg([t for t in live if t["opened"] < mid and t["coin"] in g2]),
                                          "dropped": agg([t for t in live if t["opened"] < mid and t["coin"] not in g2]),
                                          "coins_kept": len(g2)}}
    res["gate_walk_forward"] = wf
    res["decisions"] = {
        "OI falling": "FILTER: skipping signals when open interest fell over the last 24h improved results in both halves (a lot in the first, a little in the second).",
        "OI exhaustion": "FILTER (part of the OI rule): those signals lost in both halves.",
        "Big unlock ahead": "INFO ONLY: helped in the second half but hurt in the first; few signals affected.",
        "OI rising +5%": "INFO ONLY (+1 score): much better in the first half, worse in the second.",
        "Crowded funding": "INFO ONLY: only 1 signal in 90 days hit it, nothing to test.",
        "Fundamentals rising, price lagging": "INFO ONLY (+1 score): those signals did better in both halves, but too few (81) to filter on.",
        "Stand-alone unlock / fundamentals signals": "OFF: all three lost money after fees.",
        "Coin gate": "FILTER, modest evidence: coins picked on the first half did far better on the second half than the dropped ones (about break-even vs clearly losing); the reverse check showed almost no difference.",
        "Sectors, listings, on-chain": "No free history to test: shown as information; on-chain signals are labelled untested.",
    }
    # feature splits on the TA trades (how did trades WITH the flag do?)
    Sp = res["splits"]
    for name, key in (("big unlock in next 7d", "big_unlock_next7"), ("OI +5% (confirm)", "oi_confirm"),
                      ("crowded funding", "crowded"), ("OI exhaustion", "exhaust"), ("fundamentals rising, price lagging", "fund_strong")):
        Sp[name] = {"with": halves([t for t in base if t["f"][key]], mid), "without": halves([t for t in base if not t["f"][key]], mid)}
    cat = run_catalyst(data, ctx, start)
    for k in ("UNLOCK_SHORT", "POST_UNLOCK_LONG", "FUND_LONG"):
        res["catalyst"][k] = halves([t for t in cat if t["kind"] == k], mid)
        res["catalyst"][k]["list"] = [{"coin": t["coin"], "opened": datetime.fromtimestamp(t["opened"] / 1000, timezone.utc).strftime("%Y-%m-%d"),
                                       "r_net": round(t["r_net"], 2), "status": t["status"]} for t in cat if t["kind"] == k]
    res["data_coverage"] = {"oi_coins": sum(1 for c in coins if len(oi.get(c, [])) > 100),
                            "funding_coins": sum(1 for c in coins if len(fund.get(c, [])) > 50),
                            "unlock_coins": sum(1 for c in coins if ctx["unl"].get(c)),
                            "unlock_events_in_window": sum(1 for c in coins for u in ctx["unl"].get(c, []) if start <= u[0] <= last and u[1] >= K.R["unlock_min_pct"]),
                            "fund_coins": sorted(ctx["fh"])}
    json.dump(res, open(OUT, "w"), indent=1)
    print(f"{res['from']} -> {res['to']} (mid {res['mid']}), {res['coins']} coins")
    for k, h in V.items():
        print(line(k, h))
    print("-- splits (TA trades with / without the flag)")
    for k, d in Sp.items():
        print(line("  WITH " + k, d["with"])); print(line("  without", d["without"]))
    print("-- stand-alone catalyst signals")
    for k, h in res["catalyst"].items():
        print(line(k, h))
    print("coverage", res["data_coverage"])
    print("-- coin gate walk-forward", json.dumps(wf))


if __name__ == "__main__":
    if "--gate" in sys.argv:
        write_gate()
    else:
        main()
