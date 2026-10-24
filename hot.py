"""24h hot-mover scan on OKX public tickers. Parallel to the chart universe.

A long can fire when a USDT spot or perp book is up hard over 24h with a volume spike, even if the coin
is not in catalyst/universe.json yet (new listing, or ranked below the chart list) and even when the chart
rules skip it for volatility or for having no 45-day track record. Long only. Caps live in config.HOT.

The GeckoTerminal trending path (catalyst.refresh_onchain / run.dex_signals) is a different scan: DEX pools
that break their own 20-hour high. This one uses the OKX books the chart strategy already reads.

usage: python3 hot.py              print ticker hits and, when candles confirm, the signal plan
       python3 hot.py --no-candles ticker ranking only (no candle calls, no signals written)
"""
import math, sys
import config as C, strategy as S, universe


def counts_toward_chart_cap(s):
    """Chart-strategy open/daily caps. Hot movers have their own caps and must not use up those slots."""
    return s.get("market") != "DEX" and s.get("kind") != "HOT"


def _book(row, market):
    key = "spot" if market == "SPOT" else "perp"
    if not row.get(key):
        return None
    return {"market": market, "chg": row.get(f"chg_{key}"), "vol": row.get(f"vol_{key}") or 0,
            "age_d": row.get(f"age_{key}") or 0, "last": row.get(f"last_{key}") or 0}


def pick(rows, cfg=None):
    """Ticker filter + rank. One candidate per coin; books are the markets that pass, best move first.
    Volume-spike and ATR checks need candles (confirm)."""
    cfg = cfg or C.HOT
    out = []
    for row in rows:
        coin = row.get("coin") or ""
        if universe.excluded_symbol(coin):
            continue
        books = []
        for market in ("SPOT", "PERP"):
            b = _book(row, market)
            if not b or b["chg"] is None:
                continue
            if b["chg"] < cfg["min_chg_pct"] or b["vol"] < cfg["min_vol_usd"] or b["age_d"] < cfg["min_age_d"]:
                continue
            books.append(b)
        if not books:
            continue
        books.sort(key=lambda b: (-b["chg"], -b["vol"]))
        top = books[0]
        info = {"coin": coin, "spot": bool(row.get("spot")), "perp": bool(row.get("perp")),
                "vol_spot": row.get("vol_spot") or 0, "vol_perp": row.get("vol_perp") or 0,
                "age_spot": row.get("age_spot") or 0, "age_perp": row.get("age_perp") or 0,
                "vol24h": (row.get("vol_spot") or 0) + (row.get("vol_perp") or 0),
                "series": "PERP" if row.get("perp") else "SPOT"}
        out.append({"coin": coin, "books": books, "info": info, "market": top["market"], "chg": top["chg"],
                    "vol": top["vol"], "age_d": top["age_d"],
                    "ticker_score": top["chg"] * math.log10(max(top["vol"], 10))})
    out.sort(key=lambda c: -c["ticker_score"])
    return out


def spike_stats(done):
    """(ratio, window_hours, n_prior) or (None, None, None).

    With 4+ days of hourly bars, compare the last 24h with the average of the three days before that,
    so a move that started yesterday still counts. With 2-4 days, compare the last 24h with the 24h
    before. With 24-47 bars, compare the last 12h with the 12h before (stricter threshold in confirm).
    """
    n = len(done)
    if n >= 96:
        window, n_prior = 24, 3
        recent = done[-24:]
        blocks = [done[-24 * (i + 1):-24 * i] for i in range(1, 4)]
        prior = sum(sum(b["v"] for b in blk) for blk in blocks) / n_prior
    elif n >= 48:
        window, n_prior = 24, 1
        recent, prior = done[-24:], sum(b["v"] for b in done[-48:-24])
    elif n >= 24:
        window, n_prior = 12, 1
        recent, prior = done[-12:], sum(b["v"] for b in done[-24:-12])
    else:
        return None, None, None
    if n_prior > 1:
        recent_v = sum(b["v"] for b in recent)
    else:
        recent_v = sum(b["v"] for b in recent)
    if prior <= 0 or recent_v < 0:
        return None, None, None
    return recent_v / prior, window, n_prior


def spike_phrase(ratio, window, n_prior):
    """Plain multiple. Very large ratios (a dead book waking up) read as 20x+ so the alert stays readable."""
    if n_prior and n_prior > 1:
        base = f"the prior {n_prior} days"
    else:
        base = f"the prior {window}h"
    shown = f"{ratio:.1f}x" if ratio < 19.95 else "20x+"
    return f"{shown} {base}"


def stops(entry, atr, cfg=None):
    """(sl, tps, R) for a long. 2× 1h ATR, clamped so the stop is 4–12% from entry."""
    cfg = cfg or C.HOT
    if entry <= 0 or not atr or atr <= 0:
        return None, None, None
    dist = cfg["stop_atr"] * atr
    dist = max(cfg["stop_min_pct"] * entry, min(cfg["stop_max_pct"] * entry, dist))
    if dist <= 0 or dist >= entry:
        return None, None, None
    sl = entry - dist
    tps = [entry + dist * k for k in (1, 2, 3)]
    return sl, tps, dist


def money(x):
    return f"${x / 1e9:.1f}B" if x >= 1e9 else f"${x / 1e6:.1f}M" if x >= 1e6 else f"${x / 1e3:.0f}k"


def fmt(p):
    if p >= 1000: return f"{p:.1f}"
    if p >= 100: return f"{p:.2f}"
    if p >= 10: return f"{p:.3f}"
    if p >= 1: return f"{p:.4f}"
    return f"{p:.5g}"


def confirm(book, bars, cfg=None):
    """Candle check for one book. Returns a plan dict, or None when the spike or ATR is missing."""
    cfg = cfg or C.HOT
    done = [b for b in bars if b.get("done", True)]
    if len(done) < cfg["min_bars"]:
        return None
    ratio, window, n_prior = spike_stats(done)
    need = cfg["spike_mult"] if window == 24 else cfg["spike_mult_short"]
    if ratio is None or ratio < need:
        return None
    atr = S.atr(done, 14)[-1]
    if not atr or atr <= 0:
        return None
    close = done[-1]["c"]
    last = book.get("last") or 0
    # Prefer the live price. A ticker that disagrees with the last close by >25% is ignored.
    if last > 0 and close > 0 and abs(last - close) / close <= 0.25:
        entry = last
    else:
        entry = close
    sl, tps, R = stops(entry, atr, cfg)
    if sl is None:
        return None
    days = max(1, round(book.get("age_d") or 0))
    book_name = "spot" if book["market"] == "SPOT" else "perp"
    reasons = [
        f"24h hot mover +{book['chg']:.0f}% with volume spike ({spike_phrase(ratio, window, n_prior)})",
        f"24h volume {money(book['vol'])} on the OKX {book_name} book, listed {days} day{'s' if days != 1 else ''}",
        "Separate from the chart scan: this can fire when a coin is not in the main list, or when the chart rules skip a fast move",
        f"Stop {fmt(sl)} is 2× the 1h ATR, held between {cfg['stop_min_pct'] * 100:.0f}% and {cfg['stop_max_pct'] * 100:.0f}% of price; targets at 1R, 2R and 3R",
    ]
    return {"market": book["market"], "entry": entry, "sl": sl, "tps": tps, "R": R, "atr": atr,
            "spike": ratio, "window": window, "chg": book["chg"], "vol": book["vol"], "age_d": book.get("age_d") or 0,
            "bar_ts": done[-1]["ts"], "reasons": reasons,
            "score": book["chg"] * min(ratio, 6.0) * math.log10(max(book["vol"], 10))}


def choose_book(passed, pref):
    """Which confirmed market to trade. pref is choose_market's pick; use it when that book actually confirmed."""
    passed = list(dict.fromkeys(passed))
    if pref in passed:
        return pref
    if len(passed) == 1:
        return passed[0]
    return None


def _print_plan(plan):
    print(f"    plan {plan['market']} entry {fmt(plan['entry'])} SL {fmt(plan['sl'])} "
          f"TP1 {fmt(plan['tps'][0])} TP2 {fmt(plan['tps'][1])} TP3 {fmt(plan['tps'][2])} | {plan['reasons'][0]}")


def main():
    import okx
    rows = universe.markets()
    cands = pick(rows)
    print(f"hot movers: {len(cands)} books pass the ticker filters "
          f"(+{C.HOT['min_chg_pct']:.0f}% / ${C.HOT['min_vol_usd'] / 1e6:.1f}M / {C.HOT['min_age_d']}+ days)")
    show = cands[:15]
    for c in show:
        b = c["books"][0]
        extra = f" +{len(c['books']) - 1} other book" if len(c["books"]) > 1 else ""
        print(f"  {c['coin']:<10} {b['market']:<4} {b['chg']:+6.1f}%  vol ${b['vol'] / 1e6:.2f}M  age {b['age_d']}d{extra}")
    if "--no-candles" in sys.argv:
        return
    confirmed = 0
    for c in cands[:C.HOT["fetch_cap"]]:
        for b in c["books"][:1]:
            try:
                bars = okx.recent(c["coin"], b["market"], "1h", 120)
            except Exception as e:
                print(f"    {c['coin']} candles failed ({type(e).__name__})")
                continue
            plan = confirm(b, bars)
            if not plan:
                print(f"    {c['coin']} no volume spike or not enough bars")
                continue
            confirmed += 1
            _print_plan(plan)
    print(f"hot movers: {confirmed} of {min(len(cands), C.HOT['fetch_cap'])} fetched books confirmed (nothing sent)")


if __name__ == "__main__":
    main()
