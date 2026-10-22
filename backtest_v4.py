"""Strategy v4 backtest: richer chart reading (chartread.py) vs the v2 rule, on the ~200-coin universe.
Split halves (H1 older 45 days, H2 newer 45 days), after fees, one open signal per coin, then live caps.
Every setup (range breakout, retest, break of structure, 20-hour breakout) is simulated twice: with the old
exits (2 ATR stop, 1R/2R/3R) and with structure exits (stop beyond the nearest level, targets at the next levels).
Filters are kept only if they improve BOTH halves. An optional ML scorer (logistic regression, walk-forward weekly
retraining on signals already closed, no lookahead) is kept only if it beats the rules out-of-sample.
usage: python3 backtest_v4.py [--fresh]   -> backtest_v4.json
       python3 backtest_v4.py --gate      daily coin gate -> catalyst/coin_gate.json"""
import json, math, os, sys, time, pickle
from datetime import datetime, timezone
import numpy as np
import config as C, strategy as S, universe, chartread as CR
from backtest200 import jl, last_before, portfolio, agg, halves, line

H = S.H
OUT = os.path.join(C.DIR, "backtest_v4.json")
FEE = {"SPOT": 0.2, "PERP": 0.1}
CACHE = "/tmp/v4_cands.pkl"


def candidates(start):
    coins = universe.coins()
    btc1, btc4 = jl("BTC_PERP_1h.json"), jl("BTC_PERP_4h.json")
    btc_tr = S.trend_series(btc4); btc_tr_ts = [t for t, _ in btc_tr]
    btc_ts = [b["ts"] for b in btc1]
    out, used = [], []
    for n, c in enumerate(coins):
        inf = universe.info(c)
        m = inf.get("series") or "PERP"
        b1, b4 = jl(f"{c}_{m}_1h.json"), jl(f"{c}_{m}_4h.json")
        if not b1 or not b4 or len(b1) < 400:
            continue
        used.append(c)
        oi = jl(f"{c}_oi4h.json") or []; oi_ts = [r["ts"] for r in oi]
        P = CR.prep(b1, b4)
        sides = (1, -1) if inf.get("perp") else (1,)
        for i in range(200, len(b1) - 1):
            trig = CR.triggers(P, i, sides)
            if b1[i]["ts"] < start or not trig:
                continue
            t = b1[i]["ts"] + H; c0 = b1[i]["c"]
            k = last_before(btc_tr_ts, t); btc_trend = btc_tr[k][1] if k >= 0 else 0
            j = last_before(btc_ts, b1[i]["ts"])
            rs = ((c0 / b1[i - 168]["c"]) - (btc1[j]["c"] / btc1[j - 168]["c"])) * 100 if j >= 168 else None
            q = last_before(oi_ts, t - 4 * H)
            oi24 = (oi[q]["oi_usd"] / oi[q - 6]["oi_usd"] - 1) * 100 if q >= 6 and oi[q - 6]["oi_usd"] > 0 else None
            for setup, side, info in trig:
                rd = CR.read(P, i, side, setup, info)
                a = P["atr"][i]
                ta = CR.sim(P, i, side, c0, c0 - side * 2 * a, [c0 + side * 2 * a * k for k in (1, 2, 3)])
                ts_ = CR.sim(P, i, side, c0, rd["sl"], rd["tps"])
                if not ta["closed"] or not ts_["closed"]:
                    continue
                market = "PERP" if side == -1 or not inf.get("spot") else "SPOT"
                fa = FEE[market] / 100 * c0 / ta["R"]; fs = FEE[market] / 100 * c0 / ts_["R"]
                f = dict(rd["features"], btc=btc_trend * side, rs=(rs or 0) * side, oi=(oi24 or 0),
                         oi_known=oi24 is not None)
                out.append({"coin": c, "side": side, "setup": setup, "opened": t, "market": market,
                            "closes_atr": ta["close_ts"], "closes_st": ts_["close_ts"],
                            "r_atr": ta["result_r"], "rn_atr": ta["result_r"] - fa,
                            "r_st": ts_["result_r"], "rn_st": ts_["result_r"] - fs,
                            "f": f, "orig9": c in universe.CORE, "rs": (rs or 0)})
        if n % 25 == 0:
            print(f"  {n} coins, {len(out)} setups", flush=True)
    out.sort(key=lambda x: x["opened"])
    return out, used


def view(cands, exits, pred=lambda x: True):
    """portfolio-ready copies with r/r_net/closes for the chosen exit style"""
    k = "atr" if exits == "atr" else "st"
    return [dict(x, r=x["r_" + k], r_net=x["rn_" + k], closes=x["closes_" + k]) for x in cands if pred(x)]


FILT = {
    "1d+4h trend agree": lambda x: x["f"]["d1"] == 1 and x["f"]["t4"] == 1,
    "4h structure agrees": lambda x: x["f"]["st4"] == 1,
    "all 3 timeframes agree": lambda x: x["f"]["mtf"] == 3,
    "volume 1.5x": lambda x: x["f"]["vr"] >= 1.5,
    "stronger than BTC": lambda x: x["f"]["rs"] > 0,
    "BTC not against": lambda x: x["f"]["btc"] != -1,
    "room to next level >= 2R": lambda x: x["f"]["room"] >= 2,
    "OI not falling": lambda x: (not x["f"]["oi_known"]) or x["f"]["oi"] >= 0,
    "ATR% < 2.5": lambda x: x["f"]["atrp"] < 2.5,
    "RSI not stretched (<75)": lambda x: x["f"]["rsi"] < 75,
}
FEATS = ["d1", "t4", "st4", "vr", "atrp", "rsi", "room", "R_atr", "ema_dist", "base_L", "n_lvl_tps", "mtf", "btc", "rs", "oi"]


def X_of(xs, setups):
    rows = []
    for x in xs:
        f = x["f"]
        r = [float(f[k]) for k in FEATS]
        r[3] = math.log(max(r[3], 0.05)); r[9] = r[9] / 100
        r += [1.0 if x["setup"] == s else 0.0 for s in setups] + [1.0 if x["side"] == 1 else 0.0]
        rows.append(r)
    return np.array(rows)


def logit_fit(X, y, l2=1.0, iters=400):
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = (X - mu) / sd
    w = np.zeros(Z.shape[1]); b = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(Z @ w + b)))
        g = Z.T @ (p - y) / len(y) + l2 * w / len(y)
        w -= 0.5 * g; b -= 0.5 * (p - y).mean()
    return mu, sd, w, b


def logit_pred(m, X):
    mu, sd, w, b = m
    return 1 / (1 + np.exp(-(((X - mu) / sd) @ w + b)))


def ml_walk(cands, exits, start_ts, setups, top=0.4, min_train=300):
    """Weekly walk-forward: train on setups already closed before the week starts, keep the top `top` share
    (threshold set on the training scores). Returns the set of candidate ids taken and the per-week log."""
    k = "rn_" + ("atr" if exits == "atr" else "st"); ck = "closes_" + ("atr" if exits == "atr" else "st")
    take = set()
    wk = 7 * 24 * H
    t = start_ts
    end = max(x["opened"] for x in cands)
    while t <= end:
        tr = [x for x in cands if x[ck] <= t]
        te = [x for x in cands if t <= x["opened"] < t + wk]
        if len(tr) >= min_train and te:
            Xtr = X_of(tr, setups); y = np.array([1.0 if x[k] > 0 else 0.0 for x in tr])
            m = logit_fit(Xtr, y)
            thr = np.quantile(logit_pred(m, Xtr), 1 - top)
            pte = logit_pred(m, X_of(te, setups))
            for x, p in zip(te, pte):
                if p >= thr:
                    take.add(id(x))
        t += wk
    return take


def main():
    btc1 = jl("BTC_PERP_1h.json"); last = btc1[-1]["ts"]
    start, mid = last - 90 * 24 * H, last - 45 * 24 * H
    t0 = time.time()
    if os.path.exists(CACHE) and "--fresh" not in sys.argv:
        cands, used = pickle.load(open(CACHE, "rb"))
    else:
        cands, used = candidates(start)
        pickle.dump((cands, used), open(CACHE, "wb"))
    setups = sorted({x["setup"] for x in cands})
    print(f"{len(used)} coins, {len(cands)} setups, {time.time() - t0:.0f}s; by setup:",
          {s: sum(1 for x in cands if x["setup"] == s) for s in setups})
    res = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "coins": len(used),
           "from": datetime.fromtimestamp(start / 1000, timezone.utc).date().isoformat(),
           "to": datetime.fromtimestamp(last / 1000, timezone.utc).date().isoformat(),
           "mid": datetime.fromtimestamp(mid / 1000, timezone.utc).date().isoformat(),
           "fees_pct_round_trip": FEE, "rows": {}}
    R = res["rows"]
    def run(name, exits, pred, **kw):
        R[name] = halves(portfolio(view(cands, exits, pred), lambda x: True, **kw), mid)
        return R[name]
    base = run("v2 rule (20h breakout), ATR exits", "atr", lambda x: x["setup"] == "BREAKOUT20")
    run("v2 rule, structure exits", "st", lambda x: x["setup"] == "BREAKOUT20")
    best_setup = {}
    for s in setups:
        for ex in ("atr", "st"):
            h = run(f"{s}, {'ATR' if ex == 'atr' else 'structure'} exits", ex, lambda x, s=s: x["setup"] == s)
            best_setup[(s, ex)] = h
    # pick setups/exits: positive or better than v2 in both halves
    def score(h): return min(h["H1"].get("avg_r_net", -9), h["H2"].get("avg_r_net", -9))
    ranked = sorted(best_setup.items(), key=lambda kv: -score(kv[1]))
    print("setups ranked by worst-half net R:")
    for (s, ex), h in ranked:
        print(f"   {s:12} {ex:4} worst-half {score(h):+.3f}")
    # chosen exit style = the one with better worst-half over all setups combined
    allh = {ex: halves(portfolio(view(cands, ex), lambda x: True), mid) for ex in ("atr", "st")}
    exits = max(allh, key=lambda e: score(allh[e]))
    R[f"all setups, ATR exits"] = allh["atr"]; R[f"all setups, structure exits"] = allh["st"]
    keep_setups = [s for s in setups if all(best_setup[(s, exits)][h].get("avg_r_net", -9) > allh[exits][h]["avg_r_net"] - 0.0 or
                                            best_setup[(s, exits)][h].get("avg_r_net", -9) > 0 for h in ("H1", "H2"))]
    if not keep_setups:
        keep_setups = setups
    SP = lambda x: x["setup"] in keep_setups
    sb = R[f"setups {'+'.join(keep_setups)}, {exits} exits"] = halves(portfolio(view(cands, exits, SP), lambda x: True), mid)
    # single filters on top of the chosen setups
    for n, fn in FILT.items():
        R[f"  + {n}"] = halves(portfolio(view(cands, exits, lambda x: SP(x) and fn(x)), lambda x: True), mid)
    kept = [n for n in FILT if all(R[f"  + {n}"][h].get("avg_r_net", -9) > sb[h].get("avg_r_net", -9) and
                                   R[f"  + {n}"][h]["trades"] >= 30 for h in ("H1", "H2"))]
    # greedy stacking: add kept filters one by one only while both halves still improve
    stack, cur = [], sb
    for n in sorted(kept, key=lambda n: -score(R[f"  + {n}"])):
        trial = stack + [n]
        P = lambda x, tr=trial: SP(x) and all(FILT[k](x) for k in tr)
        h = halves(portfolio(view(cands, exits, P), lambda x: True), mid)
        if all(h[q].get("avg_r_net", -9) > cur[q].get("avg_r_net", -9) and h[q]["trades"] >= 30 for q in ("H1", "H2")):
            stack, cur = trial, h
    RULE = lambda x: SP(x) and all(FILT[k](x) for k in stack)
    R["v4 rules = " + (" + ".join(stack) or "setups only")] = cur
    capped = portfolio(view(cands, exits, RULE), lambda x: True, C.MAX_OPEN, C.MAX_NEW_PER_DAY)
    R[f"v4 rules + caps ({C.MAX_OPEN} open, {C.MAX_NEW_PER_DAY}/day)"] = halves(capped, mid)
    # honest out-of-sample check: choose filters on H1 only, then test that exact rule on H2
    def h1(p): return agg([x for x in portfolio(view(cands, exits, p), lambda x: True) if x["opened"] < mid])
    def h2(p): return agg([x for x in portfolio(view(cands, exits, p), lambda x: True) if x["opened"] >= mid])
    b1 = h1(SP); stack1, cur1 = [], b1
    for n in sorted(FILT, key=lambda n: -h1(lambda x, n=n: SP(x) and FILT[n](x)).get("avg_r_net", -9)):
        trial = stack1 + [n]
        hh = h1(lambda x, tr=trial: SP(x) and all(FILT[k](x) for k in tr))
        if hh.get("avg_r_net", -9) > cur1.get("avg_r_net", -9) and hh["trades"] >= 100:
            stack1, cur1 = trial, hh
    P1 = lambda x: SP(x) and all(FILT[k](x) for k in stack1)
    R["OUT-OF-SAMPLE: filters picked on H1 only (" + "+".join(stack1) + "), tested on H2"] = {"all": cur1, "H1": cur1, "H2": h2(P1)}
    res["oos_stack_h1"] = stack1
    # cap ranking: which signal goes first when the caps bind (tested, not just assumed)
    for nm, key in (("relative strength", lambda x: x["side"] * x["rs"]), ("room to next level", lambda x: x["f"]["room"]),
                    ("volume", lambda x: x["f"]["vr"])):
        vv = [dict(x, rs=key(x) * x["side"]) for x in view(cands, exits, RULE)]
        R[f"v4 rules + caps, rank by {nm}"] = halves(portfolio(vv, lambda x: True, C.MAX_OPEN, C.MAX_NEW_PER_DAY), mid)
    # ML walk-forward (out-of-sample only from when enough history exists)
    ml_start = start + 21 * 24 * H
    taken = ml_walk(cands, exits, ml_start, setups)
    ml = portfolio(view(cands, exits, lambda x: id(x) in taken), lambda x: True)
    rules_same = portfolio(view(cands, exits, lambda x: RULE(x) and x["opened"] >= ml_start), lambda x: True)
    R["ML score (walk-forward, top 40%), all setups"] = halves(ml, mid)
    R["v4 rules, same period as ML"] = halves(rules_same, mid)
    takenR = ml_walk([x for x in cands if RULE(x)], exits, ml_start, setups, top=0.6, min_train=150)
    R["v4 rules + ML score (top 60%)"] = halves(portfolio(view(cands, exits, lambda x: id(x) in takenR), lambda x: True), mid)
    ml_wins = all(R["v4 rules + ML score (top 60%)"][q].get("avg_r_net", -9) > R["v4 rules, same period as ML"][q].get("avg_r_net", -9)
                  for q in ("H1", "H2"))
    # walk-forward coin gate on the final rules
    v = portfolio(view(cands, exits, RULE), lambda x: True)
    n1, n2 = {}, {}
    for t in v:
        d = n1 if t["opened"] < mid else n2
        d[t["coin"]] = d.get(t["coin"], 0) + t["r_net"]
    g1 = {c for c, x in n1.items() if x > 0}; g2 = {c for c, x in n2.items() if x > 0}
    vv = view(cands, exits, RULE)
    h2g = portfolio([x for x in vv if x["opened"] >= mid], lambda x: True, C.MAX_OPEN, C.MAX_NEW_PER_DAY, coins=g1)
    h1g = portfolio([x for x in vv if x["opened"] < mid], lambda x: True, C.MAX_OPEN, C.MAX_NEW_PER_DAY, coins=g2)
    R["v4 rules + caps + coin gate (walk-forward)"] = {"all": agg(h1g + h2g), "H1": agg(h1g), "H2": agg(h2g)}
    res.update(exits=exits, setups=keep_setups, filters=stack, single_filters_kept=kept, ml_kept=ml_wins,
               setup_scores={f"{s}|{e}": score(h) for (s, e), h in best_setup.items()})
    json.dump(res, open(OUT, "w"), indent=1)
    print(f"{res['from']} -> {res['to']} (mid {res['mid']})")
    for k, h in R.items():
        print(line(k[:44], h))
    print("exits:", exits, "setups:", keep_setups, "stack:", stack, "ML kept:", ml_wins)


def live_rule(x):
    V = C.V4
    return (x["setup"] in V["setups"] and x["f"]["room"] >= V["min_room_r"] and x["f"]["atrp"] < V["max_atr_pct"]
            and (not C.CATALYST_FILTERS.get("oi_falling") or not x["f"]["oi_known"] or x["f"]["oi"] >= 0))


def write_gate(days=45):
    """Daily walk-forward coin gate on the live v4 rules: net R per coin after fees over the last `days`."""
    import catalyst as K
    last = jl("BTC_PERP_1h.json")[-1]["ts"]
    cands, used = candidates(last - days * 24 * H)
    ex = "atr" if C.V4["exits"] == "atr" else "st"
    taken = portfolio(view(cands, ex, live_rule), lambda x: True)
    net = {c: 0.0 for c in used}
    for t in taken:
        net[t["coin"]] += t["r_net"]
    net = {c: round(v, 2) for c, v in net.items()}
    K.jsave("coin_gate", {"updated": int(time.time()), "days": days, "net_r": net, "strategy": "v4",
                          "allowed": sorted(c for c, v in net.items() if v > 0),
                          "rule": f"chart signals only on coins with a positive result after fees over the last {days} days"})
    print(f"gate: {sum(1 for v in net.values() if v > 0)} of {len(used)} coins allowed ({len(taken)} signals)")


if __name__ == "__main__":
    if "--gate" in sys.argv:
        write_gate()
    else:
        main()
