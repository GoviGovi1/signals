"""Unit checks for the OKX universe rules. No network."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import universe as U


def _row(coin, vol_spot=0, vol_perp=0, age_spot=0, age_perp=0, spot=True, perp=True):
    return {"coin": coin, "spot": spot, "perp": perp, "vol_spot": vol_spot, "vol_perp": vol_perp,
            "age_spot": age_spot, "age_perp": age_perp}


def test_thresholds_and_rule():
    assert U.SIZE == 350 and U.KEEP_TOP == 450 and U.MIN_AGE_D == 30 and U.MIN_VOL == 2.5e5
    rule = U.rule_text()
    assert "up to 350" in rule and "$0.25M/day" in rule and "30 days" in rule and "top 450" in rule
    assert "no stablecoins" in rule
    for c in U.CORE:
        assert c in U.CORE and not U.excluded_symbol(c)


def test_exclusions():
    assert U.excluded_symbol("USDT") and U.excluded_symbol("WBTC") and U.excluded_symbol("WETH")
    assert U.excluded_symbol("BTC3L") and U.excluded_symbol("ETHUP") and U.excluded_symbol("BULL")
    assert U.excluded_symbol("X" * 16) and U.excluded_symbol("") and U.excluded_symbol("bad")
    assert not U.excluded_symbol("WET") and not U.excluded_symbol("SUPER") and not U.excluded_symbol("PEPE")


def test_qualify_age_and_volume():
    young_perp = _row("AAA", vol_spot=U.MIN_VOL, vol_perp=U.MIN_VOL, age_spot=U.MIN_AGE_D, age_perp=U.MIN_AGE_D - 1)
    old_perp = _row("BBB", vol_spot=1, vol_perp=U.MIN_VOL, age_spot=1, age_perp=U.MIN_AGE_D, spot=True, perp=True)
    dust = _row("CCC", vol_spot=U.MIN_VOL - 1, age_spot=U.MIN_AGE_D, age_perp=0, perp=False)
    too_new = _row("DDD", vol_spot=U.MIN_VOL * 2, age_spot=U.MIN_AGE_D - 1, perp=False)
    ok = {r["coin"]: r for r in U.qualify([young_perp, old_perp, dust, too_new])}
    assert ok["AAA"]["series"] == "SPOT"          # perp exists but is not old enough to chart
    assert ok["BBB"]["series"] == "PERP"
    assert "CCC" not in ok and "DDD" not in ok
    assert ok["AAA"]["vol24h"] == U.MIN_VOL * 2


def test_select_keeps_majors_and_hysteresis():
    # size 4, keep_top 6. An old coin inside the keep band stays even when a new coin is ranked higher,
    # until the old coin falls out of the band and frees a slot.
    names = ["BTC", "ETH", "SOL", "AAA", "BBB", "CCC", "DDD"]
    ok = [{"coin": n} for n in names]          # select ranks by list order
    old = ["BTC", "ETH", "SOL", "BBB", "CCC", "DDD"]
    out = U.select(ok, old, size=4, keep_top=6)
    assert out[:2] == ["BTC", "ETH"]
    assert "BBB" in out and "AAA" not in out and "DDD" not in out and "CCC" not in out
    assert len(out) == 4
    # DDD (rank 6) fell out of the keep band, so the best new coin takes the free slot.
    out2 = U.select(ok, ["BTC", "ETH", "SOL", "DDD"], size=4, keep_top=6)
    assert out2 == ["BTC", "ETH", "SOL", "AAA"]
    # A major that fails the volume/age screen is not forced back in.
    assert "BTC" not in U.select(ok[2:], ["BTC", "SOL"], size=4, keep_top=6)


def main():
    test_thresholds_and_rule()
    test_exclusions()
    test_qualify_age_and_volume()
    test_select_keeps_majors_and_hysteresis()
    print("universe tests passed")


if __name__ == "__main__":
    main()
