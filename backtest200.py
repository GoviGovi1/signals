"""Strategy v3 backtest on the ~200-coin universe, split-period, after fees, with the live portfolio caps.
Every bar where the v2 chart rule fires becomes a candidate (outcome simulated with the same exits); each
variant is a filter on candidates, then a chronological portfolio pass applies: one open signal per coin,
optional total-open and per-day caps (candidates in the same hour ranked by relative strength).
H1 = older 45 days, H2 = newer 45 days. Coin gate is walk-forward (picked on one half, tested on the other).
usage: python3 backtest200.py          (needs `python3 bt_fetch.py --no-funding`) -> backtest200.json
       python3 backtest200.py --gate   daily coin gate -> catalyst/coin_gate.json"""
import json, os, sys, time
from datetime import datetime, timezone
import config as C, strategy as S, universe, backtest as B

H = S.H
OUT = os.path.join(C.DIR, "backtest200.json")
FEE = {"SPOT": 0.2, "PERP": 0.1}


def jl(name):
    p = os.path.join(C.CACHE_DIR, name)
    return json.load(open(p)) if os.path.exists(p) else None


def last_before(ts_list, t):
    lo, hi = 0, len(ts_list) - 1
    if not ts_list or ts_list[0] > t:
        return -1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ts_list[mid] <= t: lo = mid
        else: hi = mid - 1
    return lo


def candidates(start):
    coins = universe.coins()
    btc1, btc4 = jl("BTC_PERP_1h.json"), jl("BTC_PERP_4h.json")
    btc_tr = S.trend_series(btc4)
    btc_tr_ts = [t for t, _ in btc_tr]
    btc_ts = [b["ts"] for b in btc1]
    out, used = [], []
    for c in coins:
        inf = universe.info(c)
        m = inf.get("series") or "PERP"
        b1, b4 = jl(f"{c}_{m}_1h.json"), jl(f"{c}_{m}_4h.json")
        if not b1 or not b4 or len(b1) < 400:
            continue
        used.append(c)
        oi = jl(f"{c}_oi4h.json") or []
        oi_ts = [r["ts"] for r in oi]
        ind = S.indicators(b1, b4)
        mk = "PERP" if inf.get("perp") else "SPOT"           # shorts need a perp
        for i in range(200, len(b1) - 1):
            if b1[i]["ts"] < start:
                continue
            side = S.signal_at(b1, ind, i, mk)
            if not side:
                continue
            if side == 1 and not inf.get("spot") and not inf.get("perp"):
                continue
            t = b1[i]["ts"] + H
            c0 = b1[i]["c"]
            # BTC regime and relative strength (BTC data known at t)
            k = last_before(btc_tr_ts, t)
            btc_trend = btc_tr[k][1] if k >= 0 else 0
            j = last_before(btc_ts, b1[i]["ts"])
            rs = None
            if j >= 168 and i >= 168:
                rs = ((c0 / b1[i - 168]["c"]) - (btc1[j]["c"] / btc1[j - 168]["c"])) * 100
            vols = [b["v"] for b in b1[i - 20:i]]
            vr = b1[i]["v"] / (sum(vols) / 20) if sum(vols) > 0 else None
            q = last_before(oi_ts, t - 4 * H)
            oi24 = (oi[q]["oi_usd"] / oi[q - 6]["oi_usd"] - 1) * 100 if q >= 6 and oi[q - 6]["oi_usd"] > 0 else None
            tr = S.new_trade(side, c0, ind["atr"][i], t)
            jj = i + 1
            while jj < len(b1) and not tr["closed"]:
                if b1[jj]["ts"] + H > tr["opened"] + C.P["expire_h"] * H:
                    S.expire(tr, b1[jj - 1]["c"]); break
                S.step(tr, b1[jj]); jj += 1
            if not tr["closed"]:
                continue
            market = "PERP" if side == -1 or not inf.get("spot") else "SPOT"
            fee_r = FEE[market] / 100 * c0 / tr["R"]
            out.append({"coin": c, "side": side, "opened": t, "closes": b1[min(jj, len(b1) - 1)]["ts"] + H,
                        "r": tr["result_r"], "r_net": tr["result_r"] - fee_r, "market": market,
                        "btc": btc_trend, "rs": rs, "vr": vr, "oi": oi24, "atrp": ind["atr"][i] / c0 * 100,
                        "orig9": c in universe.CORE, "has_perp": bool(inf.get("perp"))})
    out.sort(key=lambda x: x["opened"])
    return out, used


def portfolio(cands, pred, max_open=None, per_day=None, coins=None):
    taken, busy, open_until = [], {}, []
    day_count = {}
    k = 0
    while k < len(cands):
        t = cands[k]["opened"]
        group = []
        while k < len(cands) and cands[k]["opened"] == t:
            group.append(cands[k]); k += 1
        group.sort(key=lambda x: -(x["side"] * (x["rs"] or 0)))
        open_until = [u for u in open_until if u > t]
        for x in group:
            if coins is not None and x["coin"] not in coins:
                continue
            if busy.get(x["coin"], 0) > t or not pred(x):
                continue
            d = t // (86400 * 1000)
            if max_open and len(open_until) >= max_open:
                continue
            if per_day and day_count.get(d, 0) >= per_day:
                continue
            taken.append(x)
            busy[x["coin"]] = x["closes"]
            open_until.append(x["closes"])
            day_count[d] = day_count.get(d, 0) + 1
    return taken


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
        return f"n={a['trades']:5} win={a.get('win_rate', '-'):>5}% net={a.get('avg_r_net', '-'):>7}" if a["trades"] else "n=    0"
    return f"{name:44} | ALL {s(h['all'])} | H1 {s(h['H1'])} | H2 {s(h['H2'])}"


F = {
    "BTC regime": lambda x: x["btc"] != -x["side"],                       # no longs in a BTC downtrend, no shorts in an uptrend
    "relative strength vs BTC": lambda x: x["rs"] is not None and x["side"] * x["rs"] > 0,
    "volume 1.5x": lambda x: x["vr"] is not None and x["vr"] >= 1.5,
    "OI not falling": lambda x: x["oi"] is None or x["oi"] >= 0,
    "longs only": lambda x: x["side"] == 1,
    "ATR% < 2.5 (calmer coins)": lambda x: x["atrp"] < 2.5,
}


def allp(names):
    fs = [F[n] for n in names]
    return lambda x: all(f(x) for f in fs)


def main():
    B.MAX_AGE = 48 * 3600
    btc1 = jl("BTC_PERP_1h.json")
    last = btc1[-1]["ts"]
    start, mid = last - 90 * 24 * H, last - 45 * 24 * H
    t0 = time.time()
    cands, used = candidates(start)
    print(f"{len(used)} coins, {len(cands)} candidate bars, {time.time() - t0:.0f}s")
    res = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "coins": len(used),
           "from": datetime.fromtimestamp(start / 1000, timezone.utc).date().isoformat(),
           "to": datetime.fromtimestamp(last / 1000, timezone.utc).date().isoformat(),
           "mid": datetime.fromtimestamp(mid / 1000, timezone.utc).date().isoformat(),
           "fees_pct_round_trip": FEE, "rows": {}}
    R = res["rows"]
    every = lambda x: True
    R["TA only, no caps (v2 rules, 200 coins)"] = halves(portfolio(cands, every), mid)
    R["TA only, original 9 coins"] = halves(portfolio(cands, lambda x: x["orig9"]), mid)
    for n in F:
        R[f"+ {n}"] = halves(portfolio(cands, F[n]), mid)
    # filters that improve BOTH halves on their own are kept
    base = R["TA only, no caps (v2 rules, 200 coins)"]
    keep = [n for n in F if n != "longs only" and all(R[f"+ {n}"][h].get("avg_r_net", -9) > base[h]["avg_r_net"] for h in ("H1", "H2"))]
    res["kept_filters"] = keep
    P = allp(keep)
    R["v3 = TA + " + " + ".join(keep)] = halves(portfolio(cands, P), mid)
    capped = portfolio(cands, P, C.MAX_OPEN, C.MAX_NEW_PER_DAY)
    R[f"v3 + caps ({C.MAX_OPEN} open, {C.MAX_NEW_PER_DAY}/day)"] = halves(capped, mid)
    # walk-forward coin gate on the v3 (uncapped) signals
    v3 = portfolio(cands, P)
    net1, net2 = {}, {}
    for t in v3:
        d = net1 if t["opened"] < mid else net2
        d[t["coin"]] = d.get(t["coin"], 0) + t["r_net"]
    g1 = {c for c, v in net1.items() if v > 0}
    g2 = {c for c, v in net2.items() if v > 0}
    h2_gate = portfolio([c for c in cands if c["opened"] >= mid], P, C.MAX_OPEN, C.MAX_NEW_PER_DAY, coins=g1)
    h1_gate = portfolio([c for c in cands if c["opened"] < mid], P, C.MAX_OPEN, C.MAX_NEW_PER_DAY, coins=g2)
    R["v3 + caps + coin gate (walk-forward)"] = {"all": agg(h1_gate + h2_gate), "H1": agg(h1_gate), "H2": agg(h2_gate),
                                                 "note": "H2 uses coins picked on H1; H1 uses coins picked on H2"}
    h2_nogate = portfolio([c for c in cands if c["opened"] >= mid], P, C.MAX_OPEN, C.MAX_NEW_PER_DAY)
    R["v3 + caps, H2 only (same start as gate test)"] = {"all": agg(h2_nogate), "H1": {"trades": 0}, "H2": agg(h2_nogate)}
    res["gate_coins"] = {"picked_on_H1": len(g1), "picked_on_H2": len(g2)}
    json.dump(res, open(OUT, "w"), indent=1)
    print(f"{res['from']} -> {res['to']} (mid {res['mid']})")
    for k, h in R.items():
        print(line(k, h))
    print("kept filters:", keep, res["gate_coins"])


def write_gate(days=45):
    """Daily walk-forward coin gate for v3: live rules (v3 filters on in config + OI-falling veto) over the last
    `days`, one open signal per coin, net R per coin after fees -> catalyst/coin_gate.json."""
    import catalyst as K
    btc1 = jl("BTC_PERP_1h.json")
    start = btc1[-1]["ts"] - days * 24 * H
    cands, used = candidates(start)
    names = [n for n, on in getattr(C, "V3_FILTERS", {}).items() if on and n in F]
    if C.CATALYST_FILTERS.get("oi_falling"):
        names.append("OI not falling")
    taken = portfolio(cands, allp(names))
    net = {c: 0.0 for c in used}
    for t in taken:
        net[t["coin"]] += t["r_net"]
    net = {c: round(v, 2) for c, v in net.items()}
    K.jsave("coin_gate", {"updated": int(time.time()), "days": days, "net_r": net, "rules": names,
                          "allowed": sorted(c for c, v in net.items() if v > 0),
                          "rule": f"chart signals only on coins with a positive result after fees over the last {days} days"})
    print(f"gate: {sum(1 for v in net.values() if v > 0)} of {len(used)} coins allowed ({len(taken)} signals, rules {names})")


if __name__ == "__main__":
    if "--gate" in sys.argv:
        write_gate()
    else:
        main()
