"""Unit checks for the hot-mover filters, ranking, volume spike and stops. No network."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hot

CFG = {"min_chg_pct": 20, "min_vol_usd": 500_000, "min_age_d": 2, "min_bars": 30,
       "spike_mult": 1.8, "spike_mult_short": 2.2, "stop_atr": 2.0, "stop_min_pct": 0.04, "stop_max_pct": 0.12}


def row(coin, chg_spot=None, vol_spot=0, age_spot=0, spot=False, chg_perp=None, vol_perp=0, age_perp=0, perp=False,
        last_spot=1, last_perp=1):
    return {"coin": coin, "spot": spot, "perp": perp, "vol_spot": vol_spot, "vol_perp": vol_perp,
            "age_spot": age_spot, "age_perp": age_perp, "chg_spot": chg_spot, "chg_perp": chg_perp,
            "last_spot": last_spot, "last_perp": last_perp}


def bars(n, vol, close=100.0, noise=1.0, start=1_700_000_000_000):
    out = []
    for i in range(n):
        v = vol[i] if isinstance(vol, list) else vol
        out.append({"ts": start + i * 3_600_000, "o": close, "h": close + noise, "l": close - noise,
                    "c": close, "v": v, "done": True})
    return out


def test_pick_filters_and_rank():
    rows = [
        row("WET", chg_spot=42, vol_spot=2_000_000, age_spot=10, spot=True, last_spot=0.07),
        row("USDT", chg_spot=50, vol_spot=9_000_000, age_spot=100, spot=True),
        row("BTC3L", chg_spot=80, vol_spot=9_000_000, age_spot=100, spot=True),
        row("NEW", chg_spot=60, vol_spot=3_000_000, age_spot=1, spot=True),
        row("DUST", chg_spot=40, vol_spot=100_000, age_spot=20, spot=True),
        row("SLOW", chg_spot=15, vol_spot=5_000_000, age_spot=40, spot=True),
        row("BIG", chg_spot=25, vol_spot=10_000_000, age_spot=40, spot=True, chg_perp=22, vol_perp=8_000_000, age_perp=40, perp=True),
        row("PERP", chg_spot=5, vol_spot=50_000, age_spot=10, spot=True, chg_perp=55, vol_perp=600_000, age_perp=8, perp=True),
    ]
    got = {c["coin"]: c for c in hot.pick(rows, CFG)}
    assert set(got) == {"WET", "BIG", "PERP"}
    assert got["WET"]["market"] == "SPOT" and got["WET"]["books"][0]["last"] == 0.07
    assert [b["market"] for b in got["BIG"]["books"]] == ["SPOT", "PERP"]
    assert got["PERP"]["books"] == [b for b in got["PERP"]["books"] if b["market"] == "PERP"]
    order = [c["coin"] for c in hot.pick(rows, CFG)]
    # 55% on $0.6M outranks 25% on $10M: the move matters more than a modest size gap, and both clear the floor.
    assert order[0] == "PERP"
    assert got["WET"]["info"]["spot"] is True and got["WET"]["info"]["perp"] is False


def test_spike_and_stops():
    quiet = bars(72, 10) + bars(24, 40, start=1_700_000_000_000 + 72 * 3_600_000)
    ratio, window, n_prior = hot.spike_stats(quiet)
    assert window == 24 and n_prior == 3 and abs(ratio - 4.0) < 1e-9
    assert hot.spike_phrase(ratio, window, n_prior) == "4.0x the prior 3 days"
    assert hot.spike_phrase(1988.1, 24, 3) == "20x+ the prior 3 days"
    day = bars(24, 10) + bars(24, 30, start=1_700_000_000_000 + 24 * 3_600_000)
    ratio, window, n_prior = hot.spike_stats(day)
    assert (window, n_prior) == (24, 1) and abs(ratio - 3.0) < 1e-9
    short = bars(18, 10) + bars(12, 50, start=1_700_000_000_000 + 18 * 3_600_000)
    ratio, window, n_prior = hot.spike_stats(short)
    assert window == 12 and n_prior == 1 and ratio > 2.2
    assert hot.spike_stats(bars(20, 10)) == (None, None, None)
    assert hot.spike_stats(bars(48, 0))[0] is None

    sl, tps, R = hot.stops(100, 1, CFG)       # 2×ATR = 2% → clamp up to 4%
    assert abs(sl - 96) < 1e-9 and abs(R - 4) < 1e-9 and [round(x, 6) for x in tps] == [104, 108, 112]
    sl, tps, R = hot.stops(100, 10, CFG)      # 2×ATR = 20% → clamp down to 12%
    assert abs(sl - 88) < 1e-9 and abs(R - 12) < 1e-9 and [round(x, 6) for x in tps] == [112, 124, 136]
    sl, tps, R = hot.stops(100, 3, CFG)       # 6% sits inside the band
    assert abs(sl - 94) < 1e-9 and [round(x, 6) for x in tps] == [106, 112, 118]
    assert hot.stops(0, 1, CFG)[0] is None


def test_confirm_reason_and_choose_book():
    book = {"market": "SPOT", "chg": 42.4, "vol": 2_000_000, "age_d": 10, "last": 100}
    vol = [10] * 72 + [40] * 24
    plan = hot.confirm(book, bars(96, vol), CFG)
    assert plan["reasons"][0] == "24h hot mover +42% with volume spike (4.0x the prior 3 days)"
    assert "OKX spot book" in plan["reasons"][1] and "listed 10 days" in plan["reasons"][1]
    assert plan["entry"] == 100 and abs(plan["sl"] - 96) < 1e-6
    assert plan["tps"][0] > plan["entry"] > plan["sl"]
    assert abs(plan["tps"][2] - plan["entry"] - 3 * (plan["entry"] - plan["sl"])) < 1e-6
    # live price used when it is close to the close; a wild ticker falls back to the close
    plan2 = hot.confirm(dict(book, last=110), bars(96, vol, close=100), CFG)
    assert plan2["entry"] == 110
    plan3 = hot.confirm(dict(book, last=140), bars(96, vol, close=100), CFG)
    assert plan3["entry"] == 100
    weak = [10] * 72 + [15] * 24
    assert hot.confirm(book, bars(96, weak), CFG) is None
    assert hot.confirm(book, bars(20, 10), CFG) is None
    short = [10] * 18 + [50] * 12
    plan_s = hot.confirm(book, bars(30, short), CFG)
    assert plan_s and "prior 12h" in plan_s["reasons"][0]
    assert hot.confirm(book, bars(30, [10] * 18 + [20] * 12), CFG) is None   # 2.0x < 2.2x short-window bar

    assert hot.choose_book(["PERP"], "SPOT") == "PERP"
    assert hot.choose_book(["SPOT", "PERP"], "SPOT") == "SPOT"
    assert hot.choose_book(["SPOT", "PERP"], None) is None
    assert hot.choose_book([], "SPOT") is None
    assert hot.counts_toward_chart_cap({"market": "SPOT", "kind": "TA"})
    assert hot.counts_toward_chart_cap({"market": "PERP", "kind": "TA"})
    assert not hot.counts_toward_chart_cap({"market": "SPOT", "kind": "HOT"})
    assert not hot.counts_toward_chart_cap({"market": "DEX", "kind": "ONCHAIN"})


def main():
    test_pick_filters_and_rank()
    test_spike_and_stops()
    test_confirm_reason_and_choose_book()
    print("hot mover tests passed")


if __name__ == "__main__":
    main()
