"""Backtest the live strategy on the last ~90 days of 1h candles per coin and market (OKX data).
usage: python3 backtest.py [--refresh]   writes backtest.json, prints a summary.
Numbers are what the rules produced on past data; fees shown separately; funding/slippage ignored."""
import json, os, sys, time
from datetime import datetime, timezone
import config as C, okx, strategy as S

DAYS = 90
H = S.H


def load(coin, market, bar, n, refresh):
    path = os.path.join(C.CACHE_DIR, f"{coin}_{market}_{bar}.json")
    if not refresh and os.path.exists(path) and time.time() - os.path.getmtime(path) < 6 * 3600:
        return json.load(open(path))
    rows = okx.history(coin, market, bar, n)
    json.dump(rows, open(path, "w"))
    return rows


def run(params, data, start_ms):
    trades = []
    for (coin, market), (b1, b4) in data.items():
        ind = S.indicators(b1, b4, params)
        i, n = 0, len(b1)
        while i < n - 1:
            side = S.signal_at(b1, ind, i, market, params) if b1[i]["ts"] >= start_ms else 0
            if not side:
                i += 1; continue
            t = S.new_trade(side, b1[i]["c"], ind["atr"][i], b1[i]["ts"] + H, params)
            j = i + 1
            while j < n and not t["closed"]:
                if b1[j]["ts"] + H > t["opened"] + params["expire_h"] * H:
                    S.expire(t, b1[j - 1]["c"]); break
                S.step(t, b1[j]); j += 1
            if not t["closed"]:
                break                                  # still open at the end of the data: not counted
            fee_r = C.FEES_PCT[market] / 100 * t["entry"] / t["R"]
            trades.append({"coin": coin, "market": market, "side": "LONG" if side == 1 else "SHORT",
                           "opened": t["opened"], "status": t["status"], "r": t["result_r"],
                           "r_net": t["result_r"] - fee_r, "pct": S.realized_pct(t), "hits": t["hits"]})
            i = j                                      # one open signal per coin per market
    return trades


def summarize(trades):
    def agg(ts):
        n = len(ts)
        if not n:
            return {"trades": 0}
        w = sum(1 for t in ts if t["r"] > 0)
        return {"trades": n, "wins": w, "win_rate": round(w / n * 100, 1),
                "tp1_rate": round(sum(1 for t in ts if t["hits"] >= 1) / n * 100, 1),
                "avg_r": round(sum(t["r"] for t in ts) / n, 3), "total_r": round(sum(t["r"] for t in ts), 2),
                "avg_r_net": round(sum(t["r_net"] for t in ts) / n, 3),
                "total_r_net": round(sum(t["r_net"] for t in ts), 2),
                "stopped": sum(1 for t in ts if t["status"] == "stopped"),
                "tp3": sum(1 for t in ts if t["status"] == "tp3"),
                "expired": sum(1 for t in ts if t["status"] == "expired")}
    out = {"all": agg(trades)}
    for m in C.MARKETS:
        out[m] = agg([t for t in trades if t["market"] == m])
    out["PERP_LONG"] = agg([t for t in trades if t["market"] == "PERP" and t["side"] == "LONG"])
    out["PERP_SHORT"] = agg([t for t in trades if t["market"] == "PERP" and t["side"] == "SHORT"])
    out["coins"] = {c: agg([t for t in trades if t["coin"] == c]) for c in C.COINS}
    return out


def fetch_all(refresh=False):
    data = {}
    for coin in C.COINS:
        for m in C.MARKETS:
            b1 = load(coin, m, "1h", DAYS * 24 + 300, refresh)
            b4 = load(coin, m, "4h", DAYS * 6 + 260, refresh)
            data[(coin, m)] = (b1, b4)
    return data


def main():
    data = fetch_all("--refresh" in sys.argv)
    last = max(b1[-1]["ts"] for b1, _ in data.values())
    start = last - DAYS * 24 * H
    trades = run(C.P, data, start)
    s = summarize(trades)
    res = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "days": DAYS,
           "from": datetime.fromtimestamp(start / 1000, timezone.utc).date().isoformat(),
           "to": datetime.fromtimestamp(last / 1000, timezone.utc).date().isoformat(),
           "source": "OKX public candles", "params": C.P, "fees_pct_round_trip": C.FEES_PCT, "summary": s}
    json.dump(res, open(C.BACKTEST_JSON, "w"), indent=1)
    for k in ["all", "SPOT", "PERP", "PERP_LONG", "PERP_SHORT"]:
        print(k, s[k])
    for c, v in s["coins"].items():
        print(" ", c, v)


if __name__ == "__main__":
    main()
