"""Small, pre-chosen set of variants. Judged on first half (tune) and second half (check) separately."""
import copy, json
import config as C, backtest as B, strategy as S

data = B.fetch_all(False)
last = max(b1[-1]["ts"] for b1, _ in data.values())
start = last - 90 * 24 * S.H
mid = last - 45 * 24 * S.H

orig_signal = S.signal_at
def make_signal(bull_body, above_slow):
    def sig(b1, ind, i, market, p=C.P):
        s = orig_signal(b1, ind, i, market, p)
        b = b1[i]
        if s and bull_body and (b["c"] - b["o"]) * s <= 0:
            return 0
        if s and above_slow and (b["c"] - ind["es"][i]) * s <= 0:
            return 0
        return s
    return sig

variants = {
  "baseline": ({}, False, False),
  "stop2.0": ({"atr_mult": 2.0}, False, False),
  "stop2.5": ({"atr_mult": 2.5}, False, False),
  "bodyconfirm": ({}, True, False),
  "stop2.0+body": ({"atr_mult": 2.0}, True, False),
  "rsi55": ({"rsi_long": (55, 70), "rsi_short": (30, 45)}, False, False),
  "stop2.0+rsi55": ({"atr_mult": 2.0, "rsi_long": (55, 70), "rsi_short": (30, 45)}, False, False),
}
rows = {}
for name, (over, body, slow) in variants.items():
    p = copy.deepcopy(C.P); p.update(over)
    S.signal_at = make_signal(body, slow)
    B.S.signal_at = S.signal_at
    tr = B.run(p, data, start)
    a = B.summarize([t for t in tr if t["opened"] < mid])
    b = B.summarize([t for t in tr if t["opened"] >= mid])
    f = B.summarize(tr)
    rows[name] = f
    def s(x, k):
        return f"n={x[k]['trades']} win={x[k].get('win_rate')}% avgR={x[k].get('avg_r')} net={x[k].get('avg_r_net')}"
    print(f"{name:14} | 1st half {s(a,'all')} | 2nd half {s(b,'all')} | spot {f['SPOT'].get('avg_r_net')} perp {f['PERP'].get('avg_r_net')}")
json.dump(rows, open("tune_results.json", "w"), indent=1)
