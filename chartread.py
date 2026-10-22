"""Chart reading for strategy v4: support/resistance, market structure, bases/ranges, setups, stops and targets.
Only uses data the live scan also has (last 300 1h candles + last 300 4h candles, closed only), so the
backtest and live signals read the chart the same way. No lookahead: a swing point at bar j is only
"known" once k later bars have closed.

  prep(b1, b4)            -> per-coin cache (indicators, pivots, rolling ranges)
  triggers(P, i, side_ok) -> list of setups firing on closed 1h bar i: RANGE_BREAK, RETEST, BOS, BREAKOUT20
  read(P, i, side, setup) -> {features, levels, stop, tps, reasons, plot}"""
import math
import numpy as np
import strategy as S

H = S.H
W1, W4 = 300, 300            # live window sizes
K1, K4 = 3, 2                # swing confirmation bars (1h, 4h)
BASE_L = (48, 96, 168, 240)  # range/base lengths tried (hours)
BASE_K = 0.8                 # base: height <= BASE_K * sqrt(L) * ATR (a compressed range)


def _pivots(h, l, k):
    hi, lo = [], []
    n = len(h)
    for j in range(k, n - k):
        if h[j] == h[j - k:j + k + 1].max():
            hi.append(j)
        if l[j] == l[j - k:j + k + 1].min():
            lo.append(j)
    return hi, lo


def _roll(a, L, fn):
    out = np.full(len(a), np.nan)
    if len(a) <= L:
        return out
    from numpy.lib.stride_tricks import sliding_window_view
    w = fn(sliding_window_view(a, L), axis=1)        # w[s] covers a[s:s+L]
    out[L:] = w[:len(a) - L]                          # out[i] = over a[i-L:i]  (excludes bar i)
    return out


def prep(b1, b4):
    P = {"b1": b1, "b4": b4}
    P["ind"] = S.indicators(b1, b4)
    h = np.array([b["h"] for b in b1]); l = np.array([b["l"] for b in b1])
    c = np.array([b["c"] for b in b1]); o = np.array([b["o"] for b in b1]); v = np.array([b["v"] for b in b1])
    P.update(h=h, l=l, c=c, o=o, v=v, ts=np.array([b["ts"] for b in b1]))
    P["atr"] = np.array([x if x is not None else np.nan for x in P["ind"]["atr"]], float)
    P["ph1"], P["pl1"] = _pivots(h, l, K1)
    h4 = np.array([b["h"] for b in b4]); l4 = np.array([b["l"] for b in b4]); c4 = np.array([b["c"] for b in b4])
    P.update(h4=h4, l4=l4, c4=c4, ts4=np.array([b["ts"] for b in b4]))
    P["ph4"], P["pl4"] = _pivots(h4, l4, K4)
    P["mx"] = {L: _roll(h, L, np.max) for L in BASE_L}
    P["mn"] = {L: _roll(l, L, np.min) for L in BASE_L}
    P["vavg"] = _roll(v, 20, np.mean)
    # daily candles from 4h (UTC days), close known at day end
    days = {}
    for b in b4:
        d = b["ts"] // (86400 * 1000)
        days.setdefault(d, []).append(b["c"])
    P["d_days"] = sorted(days)
    P["d_close"] = [days[d][-1] for d in P["d_days"]]
    P["events"] = []          # recent breakouts, for retests
    P["retested"] = set()
    return P


def _known4(P, t):
    """index of last 4h candle closed by time t"""
    return int(np.searchsorted(P["ts4"], t - 4 * H, side="right")) - 1


def daily_trend(P, t):
    days = [d for d in P["d_days"] if (d + 1) * 86400 * 1000 <= t][-50:]
    if len(days) < 23:
        return 0
    cl = [P["d_close"][P["d_days"].index(d)] for d in days]
    e = S.ema(cl, 20)
    if cl[-1] > e[-1] and e[-1] > e[-4]:
        return 1
    if cl[-1] < e[-1] and e[-1] < e[-4]:
        return -1
    return 0


def structure4(P, t):
    """4h market structure from the last two confirmed swing highs/lows: +1 HH+HL, -1 LH+LL, 0 mixed."""
    j4 = _known4(P, t)
    lo4 = max(0, j4 - W4 + 1)
    hs = [j for j in P["ph4"] if lo4 <= j and j + K4 <= j4]
    ls = [j for j in P["pl4"] if lo4 <= j and j + K4 <= j4]
    if len(hs) < 2 or len(ls) < 2:
        return 0, hs, ls
    hh = P["h4"][hs[-1]] > P["h4"][hs[-2]]; hl = P["l4"][ls[-1]] > P["l4"][ls[-2]]
    if hh and hl:
        return 1, hs, ls
    if not hh and not hl:
        return -1, hs, ls
    return 0, hs, ls


def levels(P, i, tol):
    """Support/resistance: clustered swing highs/lows (4h weight 2, 1h weight 1) + volume-profile nodes."""
    t = P["ts"][i] + H
    lo1 = max(0, i - W1 + 1)
    pts = []
    for j in P["ph1"]:
        if lo1 <= j and j + K1 <= i: pts.append((P["h"][j], 1.0, "swing"))
    for j in P["pl1"]:
        if lo1 <= j and j + K1 <= i: pts.append((P["l"][j], 1.0, "swing"))
    j4 = _known4(P, t)
    lo4 = max(0, j4 - W4 + 1)
    for j in P["ph4"]:
        if lo4 <= j and j + K4 <= j4: pts.append((P["h4"][j], 2.0, "4h swing"))
    for j in P["pl4"]:
        if lo4 <= j and j + K4 <= j4: pts.append((P["l4"][j], 2.0, "4h swing"))
    # volume profile of the last 300 hours: high-volume price nodes
    tp = (P["h"][lo1:i + 1] + P["l"][lo1:i + 1] + P["c"][lo1:i + 1]) / 3
    vv = P["v"][lo1:i + 1]
    if len(tp) > 50 and tp.max() > tp.min():
        hist, edges = np.histogram(tp, bins=40, weights=vv)
        med = np.median(hist[hist > 0]) if (hist > 0).any() else 0
        for b in range(1, 39):
            if hist[b] > 1.8 * med and hist[b] >= hist[b - 1] and hist[b] >= hist[b + 1]:
                pts.append(((edges[b] + edges[b + 1]) / 2, 1.5, "volume node"))
    pts.sort()
    out = []
    for p, w, src in pts:
        if out and p - out[-1]["hi"] <= tol:
            L = out[-1]
            L["price"] = (L["price"] * L["w"] + p * w) / (L["w"] + w); L["w"] += w; L["hi"] = p
            L["touches"] += 1 if src != "volume node" else 0; L["src"].add(src)
        else:
            out.append({"price": p, "w": w, "hi": p, "touches": 1 if src != "volume node" else 0, "src": {src}})
    return [L for L in out if L["w"] >= 2.0]       # need 2+ touches or a 4h swing or volume node + touch


def _base(P, i, side):
    a = P["atr"][i - 1]
    best = None
    for L in BASE_L:
        if i - L < max(0, i - W1 + 1):
            continue
        mx, mn = P["mx"][L][i], P["mn"][L][i]
        if np.isnan(mx) or np.isnan(a):
            continue
        if mx - mn <= BASE_K * math.sqrt(L) * a and (mx - mn) / P["c"][i] <= 0.25:
            best = (L, mx, mn)
    return best


def triggers(P, i, side_ok=(1, -1)):
    """Setups that fire on closed 1h bar i (each as (setup, side, info))."""
    out = []
    if i < 200 or np.isnan(P["atr"][i]):
        return out
    c, o, a = P["c"][i], P["o"][i], P["atr"][i]
    # 1) range / base breakout (like a box breakout on the chart)
    for side in side_ok:
        b = _base(P, i, side)
        if not b:
            continue
        L, mx, mn = b
        edge = mx if side == 1 else mn
        prev = P["c"][i - 1]
        if side * (c - edge) > 0.1 * a and side * (prev - edge) <= 0:
            out.append(("RANGE_BREAK", side, {"L": L, "top": mx, "bot": mn, "edge": edge}))
            P["events"].append((i, side, edge, L))
    # 2) retest of a recent breakout level that holds
    P["events"] = [e for e in P["events"] if i - e[0] <= 24]
    for (j, side, X, L) in P["events"]:
        if side not in side_ok or (j, side) in P["retested"] or i - j < 2:
            continue
        seg = P["c"][j + 1:i]
        if len(seg) and (side * (seg - X) < -0.5 * a).any():        # broke back inside: failed
            P["retested"].add((j, side)); continue
        wick = P["l"][i] if side == 1 else P["h"][i]
        if side * (wick - X) <= 0.3 * a and side * (c - X) > 0 and side * (c - o) > 0:
            out.append(("RETEST", side, {"L": L, "edge": X, "bars_since": i - j}))
            P["retested"].add((j, side))
    # 3) break of structure on 1h in the direction of 4h structure
    t = P["ts"][i] + H
    st4, _, _ = structure4(P, t)
    if st4:
        side = st4
        if side in side_ok:
            piv = P["ph1"] if side == 1 else P["pl1"]
            opp = P["pl1"] if side == 1 else P["ph1"]
            ser = P["h"] if side == 1 else P["l"]
            oser = P["l"] if side == 1 else P["h"]
            known = [j for j in piv if j + K1 <= i - 1 and j >= i - 120]
            okn = [j for j in opp if j + K1 <= i and j >= i - 120]
            if known and len(okn) >= 2:
                lvl = ser[known[-1]]
                higher_low = side * (oser[okn[-1]] - oser[okn[-2]]) > 0
                if higher_low and side * (c - lvl) > 0 and side * (P["c"][i - 1] - lvl) <= 0:
                    out.append(("BOS", side, {"edge": lvl}))
    # 4) the v2 rule (20-hour breakout + EMA50 + RSI + 4h trend)
    sv = S.signal_at(P["b1"], P["ind"], i, "PERP" if -1 in side_ok else "SPOT")
    if sv:
        out.append(("BREAKOUT20", sv, {}))
    return out


def fmtp(p):
    return f"{p:.1f}" if p >= 1000 else f"{p:.2f}" if p >= 100 else f"{p:.3f}" if p >= 10 else f"{p:.4f}" if p >= 1 else f"{p:.5g}"


def read(P, i, side, setup, info):
    """Full chart reading for a setup: features (for filters/ML), stop + targets from structure, plain-word reasons."""
    c, a = P["c"][i], P["atr"][i]
    t = P["ts"][i] + H
    lv = levels(P, i, 0.6 * a)
    # structure stop: beyond the nearest level/swing on the other side with room >= 1 ATR, 0.3 ATR buffer
    below = sorted([L["price"] for L in lv if side * (c - L["price"]) >= 1.0 * a], key=lambda p: side * (c - p))
    stop_src = "structure"
    if below and side * (c - below[0]) + 0.3 * a <= 4.0 * a:
        sl = below[0] - side * 0.3 * a
    else:
        sl = c - side * 2.0 * a; stop_src = "2 ATR"
    if setup in ("RANGE_BREAK", "RETEST") and info.get("edge") is not None:
        # under the broken range edge if that is tighter but still >= 1 ATR away
        alt = info["edge"] - side * 0.6 * a
        if side * (c - alt) >= 1.0 * a and side * (c - alt) < side * (c - sl):
            sl, stop_src = alt, "below the breakout level" if side == 1 else "above the breakdown level"
    R = abs(c - sl)
    ahead = sorted([L for L in lv if side * (L["price"] - c) > 0.3 * a], key=lambda L: side * (L["price"] - c))
    tps = []
    for L in ahead:
        tp = L["price"] - side * 0.15 * a
        d = side * (tp - c)
        if d >= R * (1.0 if not tps else 0) and (not tps or side * (tp - tps[-1]) >= 0.5 * R) and d <= 6 * R:
            tps.append(tp)
        if len(tps) == 3:
            break
    n_lvl_tps = len(tps)
    while len(tps) < 3:
        k = len(tps)
        base = max(k + 1, (side * (tps[-1] - c) / R + 1) if tps else 1)
        tps.append(c + side * base * R)
    room = side * (ahead[0]["price"] - c) / R if ahead else 6.0
    # features
    ind = P["ind"]
    st4, hs, ls = structure4(P, t)
    d1 = daily_trend(P, t)
    vr = P["v"][i] / P["vavg"][i] if P["vavg"][i] > 0 else 1.0
    e50 = ind["es"][i]
    f = {"d1": d1 * side, "t4": ind["trend"][i] * side, "st4": st4 * side, "vr": float(vr),
         "atrp": float(a / c * 100), "rsi": float(ind["rsi"][i] if side == 1 else 100 - ind["rsi"][i]),
         "room": float(min(room, 6.0)), "R_atr": float(R / a), "ema_dist": float(side * (c - e50) / a),
         "base_L": info.get("L", 0), "n_lvl_tps": n_lvl_tps,
         "mtf": int(d1 * side == 1) + int(ind["trend"][i] * side == 1) + int(st4 * side == 1)}
    # plain-word reasons
    rs = []
    if setup == "RANGE_BREAK":
        days = info["L"] / 24
        span = f"{days:.0f}-day" if days >= 2 else f"{info['L']}-hour"
        rs.append(f"{'Broke out of' if side == 1 else 'Broke down from'} a {span} range at {fmtp(info['edge'])} "
                  f"(range {fmtp(info['bot'])}–{fmtp(info['top'])}) on {vr:.1f}x volume")
    elif setup == "RETEST":
        rs.append(f"Retest of the {fmtp(info['edge'])} breakout{'' if side == 1 else 'down'} level held "
                  f"({info['bars_since']}h after the break)")
    elif setup == "BOS":
        rs.append(f"Break of structure: closed {'above the last swing high' if side == 1 else 'below the last swing low'} "
                  f"{fmtp(info['edge'])}, after a {'higher low' if side == 1 else 'lower high'}")
    else:
        rs.append(f"{'Broke the 20-hour high' if side == 1 else 'Broke the 20-hour low'} on {vr:.1f}x volume")
    tf = {1: "up", -1: "down", 0: "flat"}
    rs.append(f"Timeframes: 1d {tf[d1]}, 4h {tf[ind['trend'][i]]}, 4h structure "
              + {1: "higher highs/lows", -1: "lower highs/lows", 0: "mixed"}[st4])
    if ahead:
        nl = ahead[0]
        rs.append(f"Next {'resistance' if side == 1 else 'support'} {fmtp(nl['price'])} "
                  f"({room:.1f}R away, {'/'.join(sorted(nl['src']))})")
    else:
        rs.append("No resistance overhead in the last 50 days" if side == 1 else "No support below in the last 50 days")
    rs.append(f"Stop {fmtp(sl)} ({stop_src}); targets " + (f"at the next {n_lvl_tps} level(s)" if n_lvl_tps else "at 1R/2R/3R"))
    plot = {"levels": [round(L["price"], 10) for L in lv if abs(L["price"] - c) <= 8 * a * max(1, R / a)][:10]}
    if setup in ("RANGE_BREAK",):
        plot["range"] = (info["bot"], info["top"], int(P["ts"][i - info["L"]]))
    return {"features": f, "sl": sl, "tps": tps, "R": R, "reasons": rs, "plot": plot, "levels": lv}


def sim(P, i, side, entry, sl, tps, expire_h=48):
    """Simulate exits (thirds at each TP, stop to entry after TP1, expiry). Returns trade dict."""
    R = abs(entry - sl)
    t = {"dir": side, "entry": entry, "sl": sl, "sl0": sl, "R": R, "tps": list(tps), "hits": 0, "be_from": None,
         "opened": int(P["ts"][i]) + H, "status": "open", "closed": False, "exit_price": None, "result_r": None, "lev": 1}
    b1 = P["b1"]
    jj = i + 1
    while jj < len(b1) and not t["closed"]:
        if b1[jj]["ts"] + H > t["opened"] + expire_h * H:
            S.expire(t, b1[jj - 1]["c"]); break
        S.step(t, b1[jj]); jj += 1
    t["close_ts"] = b1[min(jj, len(b1) - 1)]["ts"] + H
    return t
