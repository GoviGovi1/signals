"""Exit-plan + quality-filter variants on the live v4 rules (backtest_v4.py candidates, structure levels).
Same 90 days, same ~200-coin universe, after fees, one open signal per coin, halves H1/H2 (+ live caps rows).
Exit variants change how the position is scaled out and when the stop moves to entry; filters add
BTC-trend alignment and a minimum reward-to-risk to TP2.  usage: python3 backtest_exits.py [--fresh]"""
import json, os, sys, time, pickle
from datetime import datetime, timezone
import numpy as np
import config as C, strategy as S, universe, chartread as CR
from backtest200 import jl, last_before, portfolio, agg

H = S.H
FEE = {"SPOT": 0.2, "PERP": 0.1}
CACHE = "/tmp/exit_cands.pkl"
OUT = os.path.join(C.DIR, "backtest_exits.json")
EXP = 48


def candidates(start):
    coins = universe.coins()
    btc4 = jl("BTC_PERP_4h.json"); btc1 = jl("BTC_PERP_1h.json")
    btc_tr = S.trend_series(btc4); btc_tr_ts = [t for t, _ in btc_tr]; btc_ts = [b["ts"] for b in btc1]
    out, used = [], []
    for n, c in enumerate(coins):
        inf = universe.info(c); m = inf.get("series") or "PERP"
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
            if i + EXP + 1 > len(b1):          # not enough future bars to finish any exit plan
                continue
            t = b1[i]["ts"] + H; c0 = b1[i]["c"]
            k = last_before(btc_tr_ts, t); btc_trend = btc_tr[k][1] if k >= 0 else 0
            j = last_before(btc_ts, b1[i]["ts"])
            rs = ((c0 / b1[i - 168]["c"]) - (btc1[j]["c"] / btc1[j - 168]["c"])) * 100 if j >= 168 else None
            q = last_before(oi_ts, t - 4 * H)
            oi24 = (oi[q]["oi_usd"] / oi[q - 6]["oi_usd"] - 1) * 100 if q >= 6 and oi[q - 6]["oi_usd"] > 0 else None
            fw = np.array([[b["ts"], b["h"], b["l"], b["c"]] for b in b1[i + 1:i + EXP + 2]], float)
            for setup, side, info in trig:
                rd = CR.read(P, i, side, setup, info)
                market = "PERP" if side == -1 or not inf.get("spot") else "SPOT"
                f = dict(rd["features"], btc=btc_trend * side, oi=(oi24 or 0), oi_known=oi24 is not None)
                out.append({"coin": c, "side": side, "setup": setup, "opened": t, "market": market, "entry": c0,
                            "sl": rd["sl"], "tps": list(rd["tps"]), "R": rd["R"], "fee_r": FEE[market] / 100 * c0 / rd["R"],
                            "f": f, "rs": rs or 0, "fw": fw})
        if n % 25 == 0:
            print(f"  {n} coins, {len(out)} setups", flush=True)
    out.sort(key=lambda x: x["opened"])
    return out, used


def sim(x, w=(1/3, 1/3, 1/3), be="tp1", tp1_min=None):
    """w: share closed at TP1/TP2/TP3. be: when the stop moves to entry ('tp1', 'tp2', '1r', '1.5r', None).
    tp1_min: push TP1 out to at least this many R (keeps TP order). Same-candle stop+target -> stop first."""
    side, e, R = x["side"], x["entry"], x["R"]
    tps = list(x["tps"])
    if tp1_min:
        tps[0] = e + side * max(side * (tps[0] - e), tp1_min * R)
        for k in (1, 2):
            if side * (tps[k] - tps[k - 1]) < 0.5 * R:
                tps[k] = tps[k - 1] + side * 0.5 * R
    sl = x["sl"]; be_from = None; hits = 0; done = 0.0; r = 0.0
    trig = {"1r": 1.0, "1.5r": 1.5}.get(be)
    for ts, h, l, c in x["fw"]:
        if ts + H > x["opened"] + EXP * H:
            r += (1 - done) * side * (prev - e) / R
            return r, ts + H
        hi, lo = (h, l) if side == 1 else (-l, -h)
        cur_sl = e if (be_from is not None and ts >= be_from) else sl
        if lo <= side * cur_sl:
            r += (1 - done) * side * (cur_sl - e) / R
            return r, ts + H
        while hits < 3 and hi >= side * tps[hits]:
            r += w[hits] * side * (tps[hits] - e) / R; done += w[hits]; hits += 1
            if be_from is None and ((be == "tp1" and hits >= 1) or (be == "tp2" and hits >= 2)):
                be_from = ts + H
        if be_from is None and trig and hi - side * e >= trig * R:
            be_from = ts + H
        if hits == 3 or done >= 1 - 1e-9:
            return r, ts + H
        prev = c
    return None, None


def dd(ts):
    cum = peak = m = 0.0
    for t in sorted(ts, key=lambda t: t["closes"]):
        cum += t["r_net"]; peak = max(peak, cum); m = max(m, peak - cum)
    return round(m, 1)


def stats(ts, mid):
    o = {}
    for k, s in (("all", ts), ("H1", [t for t in ts if t["opened"] < mid]), ("H2", [t for t in ts if t["opened"] >= mid])):
        a = agg(s); a["max_dd_r"] = dd(s) if s else 0; o[k] = a
    return o


def live_rule(x):
    V = C.V4
    return (x["setup"] in V["setups"] and x["f"]["room"] >= V["min_room_r"] and x["f"]["atrp"] < V["max_atr_pct"]
            and (not C.CATALYST_FILTERS.get("oi_falling") or not x["f"]["oi_known"] or x["f"]["oi"] >= 0))


EXITS = {
    "baseline: thirds, stop->entry after TP1": dict(),
    "A1: 25/25/50, stop->entry after TP1": dict(w=(0.25, 0.25, 0.5)),
    "A2: thirds, stop->entry after TP2": dict(be="tp2"),
    "A3: thirds, stop->entry at +1R": dict(be="1r"),
    "A4: TP1 >= 1.5R, thirds, after TP1": dict(tp1_min=1.5),
    "A5: 25/25/50, stop->entry after TP2": dict(w=(0.25, 0.25, 0.5), be="tp2"),
    "A6: TP1 >= 1.5R, 25/25/50, after TP2": dict(w=(0.25, 0.25, 0.5), be="tp2", tp1_min=1.5),
    "A7: TP1 >= 1.5R, 25/25/50, at +1.5R": dict(w=(0.25, 0.25, 0.5), be="1.5r", tp1_min=1.5),
    "A8: no TP1 (0/50/50), after TP2": dict(w=(0.0, 0.5, 0.5), be="tp2"),
}
FILTERS = {
    "": lambda x: True,
    " + BTC trend agrees": lambda x: x["f"]["btc"] == 1,
    " + RR>=2 to TP2": lambda x: abs(x["tps"][1] - x["entry"]) / x["R"] >= 2.0,
    " + BTC trend + RR>=2": lambda x: x["f"]["btc"] == 1 and abs(x["tps"][1] - x["entry"]) / x["R"] >= 2.0,
}


def main():
    last = jl("BTC_PERP_1h.json")[-1]["ts"]
    start, mid = last - 90 * 24 * H, last - 45 * 24 * H
    t0 = time.time()
    if os.path.exists(CACHE) and "--fresh" not in sys.argv:
        cands, used = pickle.load(open(CACHE, "rb"))
    else:
        cands, used = candidates(start)
        pickle.dump((cands, used), open(CACHE, "wb"))
    base = [x for x in cands if live_rule(x)]
    print(f"{len(used)} coins, {len(cands)} setups, {len(base)} pass live v4 rules, {time.time() - t0:.0f}s")
    rows = {}
    for en, kw in EXITS.items():
        vv = []
        for x in base:
            r, cl = sim(x, **kw)
            if r is not None:
                vv.append(dict(x, r=r, r_net=r - x["fee_r"], closes=cl, fw=None))
        for fn, fp in FILTERS.items():
            if fn and not en.startswith(("baseline", "A3", "A5", "A6", "A7")):
                continue
            ts = portfolio(vv, fp)
            rows[en + fn] = stats(ts, mid)
            capped = portfolio(vv, fp, C.MAX_OPEN, C.MAX_NEW_PER_DAY)
            rows[en + fn + " [caps]"] = stats(capped, mid)
    res = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "coins": len(used),
           "from": datetime.fromtimestamp(start / 1000, timezone.utc).date().isoformat(),
           "to": datetime.fromtimestamp(last / 1000, timezone.utc).date().isoformat(),
           "mid": datetime.fromtimestamp(mid / 1000, timezone.utc).date().isoformat(), "fees_pct_round_trip": FEE, "rows": rows}
    json.dump(res, open(OUT, "w"), indent=1)
    for k, h in rows.items():
        a, h1, h2 = h["all"], h["H1"], h["H2"]
        print(f"{k:58} n={a['trades']:5} win={a.get('win_rate',0):5}% avg={a.get('avg_r_net',0):+.3f} tot={a.get('total_r_net',0):+7.1f} "
              f"dd={a['max_dd_r']:6} | H1 {h1.get('avg_r_net',0):+.3f} ({h1['trades']}) | H2 {h2.get('avg_r_net',0):+.3f} ({h2['trades']})")


if __name__ == "__main__":
    main()
