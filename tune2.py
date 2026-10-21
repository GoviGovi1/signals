"""Structural variants (entry type), each tested on both halves of the 90 days."""
import copy
import config as C, backtest as B, strategy as S

data = B.fetch_all(False)
last = max(b1[-1]["ts"] for b1, _ in data.values())
start = last - 90 * 24 * S.H
mid = last - 45 * 24 * S.H
base_sig = S.signal_at

def cross(b1, ind, i, market, p=C.P):
    ef, es, t, r = ind["ef"], ind["es"], ind["trend"][i], ind["rsi"][i]
    if i < 1 or None in (ef[i], es[i], ef[i-1], es[i-1], r, ind["atr"][i]): return 0
    if t == 1 and ef[i-1] <= es[i-1] and ef[i] > es[i] and r >= 50: return 1
    if market == "PERP" and t == -1 and ef[i-1] >= es[i-1] and ef[i] < es[i] and r <= 50: return -1
    return 0

def breakout(n):
    def f(b1, ind, i, market, p=C.P):
        t, r = ind["trend"][i], ind["rsi"][i]
        if i < n or r is None or ind["atr"][i] is None or ind["es"][i] is None: return 0
        hh = max(b["h"] for b in b1[i-n:i]); ll = min(b["l"] for b in b1[i-n:i])
        c = b1[i]["c"]
        if t == 1 and c > hh and c > ind["es"][i] and 55 <= r <= 75: return 1
        if market == "PERP" and t == -1 and c < ll and c < ind["es"][i] and 25 <= r <= 45: return -1
        return 0
    return f

def pull50(b1, ind, i, market, p=C.P):
    ef, es, t, r = ind["ef"][i], ind["es"][i], ind["trend"][i], ind["rsi"][i]
    if None in (ef, es, r, ind["atr"][i]): return 0
    b = b1[i]
    if t == 1 and ef > es and b["l"] <= es < b["c"] and 45 <= r <= 65: return 1
    if market == "PERP" and t == -1 and ef < es and b["h"] >= es > b["c"] and 35 <= r <= 55: return -1
    return 0

variants = {
  "pullback20 (baseline)": (base_sig, {}),
  "ema cross": (cross, {}),
  "ema cross stop2": (cross, {"atr_mult": 2.0}),
  "breakout20": (breakout(20), {}),
  "breakout20 stop2": (breakout(20), {"atr_mult": 2.0}),
  "breakout48 stop2": (breakout(48), {"atr_mult": 2.0}),
  "pullback50": (pull50, {}),
  "pullback50 stop2": (pull50, {"atr_mult": 2.0}),
}
def s(x):
    return f"n={x['trades']:3} win={x.get('win_rate')}% avgR={x.get('avg_r')} net={x.get('avg_r_net')}"
for name, (fn, over) in variants.items():
    p = copy.deepcopy(C.P); p.update(over)
    B.S.signal_at = fn
    tr = B.run(p, data, start)
    a = B.summarize([t for t in tr if t["opened"] < mid])["all"]
    b = B.summarize([t for t in tr if t["opened"] >= mid])["all"]
    f = B.summarize(tr)
    print(f"{name:22} | H1 {s(a)} | H2 {s(b)} | SPOT net {f['SPOT'].get('avg_r_net')} PERP net {f['PERP'].get('avg_r_net')} (L {f['PERP_LONG'].get('avg_r_net')} S {f['PERP_SHORT'].get('avg_r_net')})")
