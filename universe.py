"""Coin universe: the ~40 most liquid coins that have BOTH an OKX USDT perpetual and an OKX USDT spot pair.
Refreshed once a day into catalyst/universe.json (committed), so every run uses the same list.
Rules: 24h USD volume (perp + spot) ranks the coins; stablecoins, tokenized stocks/commodities (no spot pair)
and coins whose perp is younger than 60 days (not enough history for the 4h EMA200) are left out.
The original 9 majors are always included. Hysteresis: a coin already in the list stays until it drops
out of the top 60, so the list doesn't churn every day.
usage: python3 universe.py [--force]"""
import json, os, sys, time
import config as C, okx

PATH = os.path.join(C.DIR, "catalyst", "universe.json")
CORE = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "SUI", "AVAX", "LINK"]
STABLE = {"USDC", "USDT", "DAI", "FDUSD", "TUSD", "USDE", "USDD", "PYUSD", "USDG", "RLUSD", "EURC", "USD1", "USDS",
          "BUSD", "XAUT", "PAXG", "WBTC", "STETH", "WETH", "BETH", "USDQ", "FRAX"}
SIZE, KEEP_TOP, MIN_AGE_D = 40, 60, 60


def load():
    try:
        return json.load(open(PATH))
    except (OSError, ValueError):
        return {"coins": CORE, "updated": 0}


def coins():
    return load()["coins"]


def build():
    now = time.time()
    sw = {x["instId"]: x for x in okx.get("/api/v5/public/instruments", {"instType": "SWAP"})}
    sp = {x["instId"] for x in okx.get("/api/v5/public/instruments", {"instType": "SPOT"}) if x.get("state") == "live"}
    tick = {x["instId"]: x for x in okx.get("/api/v5/market/tickers", {"instType": "SWAP"})}
    tick.update({x["instId"]: x for x in okx.get("/api/v5/market/tickers", {"instType": "SPOT"})})
    rows = []
    for iid, ins in sw.items():
        if not iid.endswith("-USDT-SWAP") or ins.get("state") != "live":
            continue
        c = iid.split("-")[0]
        if c in STABLE or f"{c}-USDT" not in sp:
            continue
        age_d = (now - int(ins.get("listTime") or 0) / 1000) / 86400
        vol = 0.0
        for i in (iid, f"{c}-USDT"):
            t = tick.get(i)
            if t:
                try:
                    q = float(t["volCcy24h"]) * float(t["last"]) if i == iid else float(t["volCcy24h"])
                    vol += q
                except (ValueError, TypeError):
                    pass
        rows.append({"coin": c, "vol24h": round(vol), "age_d": round(age_d)})
    rows.sort(key=lambda r: -r["vol24h"])
    ok = [r for r in rows if r["age_d"] >= MIN_AGE_D]
    rank = {r["coin"]: k for k, r in enumerate(ok)}
    old = load()["coins"]
    keep = [c for c in old if rank.get(c, 999) < KEEP_TOP]
    out = list(dict.fromkeys(CORE + keep))
    for r in ok:
        if len(out) >= SIZE:
            break
        if r["coin"] not in out:
            out.append(r["coin"])
    # trim if hysteresis left too many: drop the lowest-ranked non-core
    while len(out) > SIZE:
        worst = max((c for c in out if c not in CORE), key=lambda c: rank.get(c, 999))
        out.remove(worst)
    out.sort(key=lambda c: (c not in CORE, rank.get(c, 999)))
    info = {r["coin"]: r for r in ok}
    data = {"updated": int(now), "coins": out, "info": {c: info.get(c) for c in out},
            "rule": f"top {SIZE} by 24h USD volume (perp+spot) with both OKX USDT perp and spot, perp age >= {MIN_AGE_D}d"}
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    json.dump(data, open(PATH, "w"), indent=1)
    return data


def refresh(force=False):
    """Rebuild at most once a day. Returns the coin list (old list if OKX is down)."""
    u = load()
    if force or time.time() - u.get("updated", 0) > 86400:
        try:
            u = build()
        except Exception as e:
            print(f"universe: refresh failed ({type(e).__name__}), keeping {len(u['coins'])} coins")
    return u["coins"]


if __name__ == "__main__":
    print(refresh("--force" in sys.argv))
