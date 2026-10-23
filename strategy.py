"""Trend + momentum setup, shared by backtest.py and run.py.

Signal on a CLOSED 1h candle (v2, after backtest tuning; v1 was an EMA20 pullback and lost money):
  LONG : 4h EMA50 > EMA200 and 4h close > EMA200 (uptrend), 1h close above the highest high of the
         previous 20 hours (breakout) and above the 1h EMA50, RSI(14) between rsi_long (55-75).
  SHORT: mirror image (PERP only): 4h downtrend, close below the 20-hour low and the EMA50, RSI 25-45.
  SPOT is long-only.
Entry = candle close. Stop = 2 x ATR(14). TP1/TP2/TP3 = 1R/2R/3R.
Exits: one third at each TP; after TP1 the stop moves to entry; anything left after 48h closes at market.
Same-candle stop+target ambiguity is resolved as the stop first (conservative)."""
from config import P

H = 3600 * 1000


def ema(xs, n):
    out = [None] * len(xs)
    if len(xs) < n:
        return out
    k = 2 / (n + 1)
    e = sum(xs[:n]) / n
    out[n - 1] = e
    for i in range(n, len(xs)):
        e = xs[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi(cl, n=14):
    out = [None] * len(cl)
    if len(cl) <= n:
        return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = cl[i] - cl[i - 1]
        g += max(d, 0); l += max(-d, 0)
    g /= n; l /= n
    out[n] = 100 if l == 0 else 100 - 100 / (1 + g / l)
    for i in range(n + 1, len(cl)):
        d = cl[i] - cl[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
        out[i] = 100 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def atr(bars, n=14):
    out = [None] * len(bars)
    trs = []
    for i, b in enumerate(bars):
        pc = bars[i - 1]["c"] if i else b["c"]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    if len(bars) <= n:
        return out
    a = sum(trs[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(bars)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


def trend_series(bars4, p=P):
    """[(close_time_ms, trend)] trend +1 up, -1 down, 0 none."""
    cl = [b["c"] for b in bars4]
    f, s = ema(cl, p["trend_fast"]), ema(cl, p["trend_slow"])
    out = []
    for i, b in enumerate(bars4):
        t = 0
        if f[i] is not None and s[i] is not None:
            if f[i] > s[i] and cl[i] > s[i]:
                t = 1
            elif f[i] < s[i] and cl[i] < s[i]:
                t = -1
        out.append((b["ts"] + 4 * H, t))
    return out


def indicators(bars1, bars4, p=P):
    cl = [b["c"] for b in bars1]
    ind = {"ef": ema(cl, p["ema_fast"]), "es": ema(cl, p["ema_slow"]),
           "rsi": rsi(cl, p["rsi_len"]), "atr": atr(bars1, p["atr_len"]), "trend": []}
    ts4 = trend_series(bars4, p)
    j, cur = 0, 0
    for b in bars1:
        close_t = b["ts"] + H
        while j < len(ts4) and ts4[j][0] <= close_t:   # only 4h candles already closed
            cur = ts4[j][1]; j += 1
        ind["trend"].append(cur if j else 0)
    return ind


def signal_at(bars1, ind, i, market, p=P):
    """Return +1 (long), -1 (short) or 0 for closed 1h candle i."""
    n = p["breakout_n"]
    es, r, a, t = ind["es"][i], ind["rsi"][i], ind["atr"][i], ind["trend"][i]
    if i < n or None in (es, r, a):
        return 0
    c = bars1[i]["c"]
    hh = max(b["h"] for b in bars1[i - n:i])
    ll = min(b["l"] for b in bars1[i - n:i])
    if t == 1 and c > hh and c > es and p["rsi_long"][0] <= r <= p["rsi_long"][1]:
        return 1
    if market == "PERP" and t == -1 and c < ll and c < es and p["rsi_short"][0] <= r <= p["rsi_short"][1]:
        return -1
    return 0


def new_trade(side, entry, atr_v, opened_ms, p=P):
    R = p["atr_mult"] * atr_v
    tps = [entry + side * R * k for k in (1, 2, 3)]
    stop_frac = R / entry
    lev = max(1, min(p["max_lev"], int(p["lev_target_loss"] / stop_frac))) if stop_frac > 0 else 1
    return {"dir": side, "entry": entry, "sl": entry - side * R, "sl0": entry - side * R, "R": R,
            "tps": tps, "hits": 0, "be_from": None, "opened": opened_ms, "status": "open",
            "closed": False, "exit_price": None, "result_r": None, "lev": lev}


def _close(t, price, status):
    side, e, R = t["dir"], t["entry"], t["R"]
    w = t.get("w") or [1 / 3, 1 / 3, 1 / 3]            # share closed at TP1/TP2/TP3 (old signals: thirds)
    rest = 1 - sum(w[:t["hits"]])
    r = sum(w[k] * side * (t["tps"][k] - e) / R for k in range(t["hits"])) + rest * side * (price - e) / R
    t["result_r"] = round(r, 4)
    t["exit_price"] = price
    t["status"] = status
    t["closed"] = True


def realized_pct(t):
    """Weighted price move % of the whole signal (by the exit weights), unlevered."""
    if t["result_r"] is None:
        return None
    return t["result_r"] * t["R"] / t["entry"] * 100


def step(t, b):
    """Apply one candle (dict ts,o,h,l,c). Returns list of event names."""
    if t["closed"]:
        return []
    ev = []
    side = t["dir"]
    hi, lo = (b["h"], b["l"]) if side == 1 else (-b["l"], -b["h"])   # mirror shorts
    sl = side * t["sl"]
    stop_live = t["hits"] == 0 or (t["be_from"] is not None and b["ts"] >= t["be_from"])
    if stop_live and lo <= sl:
        _close(t, t["sl"], "stopped" if t["hits"] == 0 else "stopped_be")
        ev.append(t["status"])
        return ev
    while t["hits"] < 3 and hi >= side * t["tps"][t["hits"]]:
        t["hits"] += 1
        ev.append(f"tp{t['hits']}")
        if t["hits"] == 1:
            t["sl"] = t["entry"]
            t["be_from"] = b["ts"] + H          # break-even stop active from next candle
    if t["hits"] == 3:
        _close(t, t["tps"][2], "tp3")
    elif t["hits"]:
        t["status"] = f"tp{t['hits']}"
    return ev


def expire(t, price):
    _close(t, price, "expired")
