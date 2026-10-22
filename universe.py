"""Coin universe: the ~200 most traded crypto coins on OKX (USDT spot pairs and/or USDT perpetuals).
Refreshed once a day into catalyst/universe.json (committed), so every run uses the same list.
Rules: crypto only (OKX instCategory 1: no tokenized stocks/commodities), no stablecoins, wrapped or
leveraged tokens; 24h USD volume (spot + perp) of at least $0.5M; the market we read the chart from must be
at least 45 days old (enough history for the 4h EMA200). Ranked by volume; the original 9 majors always stay.
Hysteresis: a coin already in the list stays while it is in the top 260, so the list doesn't churn.
Per coin we keep which markets exist: spot-only coins can only get spot longs, perps-only coins only perps.
usage: python3 universe.py [--force]"""
import json, os, re, sys, time
import config as C, okx

PATH = os.path.join(C.DIR, "catalyst", "universe.json")
CORE = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "SUI", "AVAX", "LINK"]
STABLE = {"USDC", "USDT", "DAI", "FDUSD", "TUSD", "USDE", "USDD", "PYUSD", "USDG", "RLUSD", "EURC", "USD1", "USDS",
          "BUSD", "XAUT", "PAXG", "WBTC", "STETH", "WETH", "BETH", "USDQ", "FRAX", "USDP", "GUSD", "SUSDE", "CBBTC",
          "WSTETH", "RETH", "METH", "JITOSOL", "MSOL", "BNSOL", "SOLVBTC", "LBTC", "EUR", "AUSD", "USDQ", "USTC",
          "USDR", "USDX", "FUSD", "SUSD", "EURT", "TRYB", "BRZ", "WBNB", "WSOL", "WTRX", "WEMIX"}
LEVERAGED = re.compile(r"(\d+[LS]|UP|DOWN|BULL|BEAR)$")
SIZE, KEEP_TOP, MIN_AGE_D, MIN_VOL = 200, 260, 45, 5e5


def load():
    try:
        return json.load(open(PATH))
    except (OSError, ValueError):
        return {"coins": CORE, "updated": 0, "info": {}}


def coins():
    return load()["coins"]


def info(coin):
    """{'spot': bool, 'perp': bool, 'vol_spot', 'vol_perp', 'series': 'PERP'|'SPOT'} (defaults for old files)."""
    return load().get("info", {}).get(coin) or {"spot": True, "perp": True, "series": "PERP", "vol_spot": 0, "vol_perp": 0}


def build():
    now = time.time()
    rows = {}
    tick = {x["instId"]: x for t in ("SWAP", "SPOT") for x in okx.get("/api/v5/market/tickers", {"instType": t})}
    for t in ("SWAP", "SPOT"):
        for x in okx.get("/api/v5/public/instruments", {"instType": t}):
            iid = x["instId"]
            if x.get("state") != "live" or str(x.get("instCategory", "1")) != "1":
                continue
            if not iid.endswith("-USDT-SWAP" if t == "SWAP" else "-USDT") or (t == "SPOT" and iid.count("-") != 1):
                continue
            c = iid.split("-")[0]
            if c in STABLE or LEVERAGED.search(c) or not re.fullmatch(r"[A-Z0-9]{1,15}", c):
                continue
            k = tick.get(iid)
            try:
                vol = (float(k["volCcy24h"]) * float(k["last"])) if t == "SWAP" else float(k["volCcy24h"])
            except (TypeError, ValueError, KeyError):
                vol = 0.0
            r = rows.setdefault(c, {"coin": c, "spot": False, "perp": False, "vol_spot": 0, "vol_perp": 0,
                                    "age_spot": 0, "age_perp": 0})
            m = "perp" if t == "SWAP" else "spot"
            r[m] = True
            r[f"vol_{m}"] = round(vol)
            r[f"age_{m}"] = round((now - int(x.get("listTime") or now * 1000) / 1000) / 86400)
    ok = []
    for r in rows.values():
        r["vol24h"] = r["vol_spot"] + r["vol_perp"]
        # read the chart from the perp when it is old enough, else from spot
        r["series"] = "PERP" if r["perp"] and r["age_perp"] >= MIN_AGE_D else "SPOT" if r["spot"] and r["age_spot"] >= MIN_AGE_D else None
        if r["series"] and r["vol24h"] >= MIN_VOL:
            ok.append(r)
    ok.sort(key=lambda r: -r["vol24h"])
    rank = {r["coin"]: k for k, r in enumerate(ok)}
    old = load()["coins"]
    out = list(dict.fromkeys([c for c in CORE if c in rank] + [c for c in old if rank.get(c, 999) < KEEP_TOP]))
    for r in ok:
        if len(out) >= SIZE:
            break
        if r["coin"] not in out:
            out.append(r["coin"])
    while len(out) > SIZE:
        out.remove(max((c for c in out if c not in CORE), key=lambda c: rank.get(c, 999)))
    out.sort(key=lambda c: (c not in CORE, rank.get(c, 999)))
    info = {r["coin"]: r for r in ok}
    data = {"updated": int(now), "coins": out, "info": {c: info[c] for c in out},
            "rule": f"top {SIZE} crypto coins on OKX by 24h USD volume (spot + perp), >= $0.5M/day, chart market >= {MIN_AGE_D} days old; "
                    "no stablecoins, wrapped or leveraged tokens, no tokenized stocks"}
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    json.dump(data, open(PATH, "w"), indent=1)
    return data


def refresh(force=False):
    u = load()
    if force or time.time() - u.get("updated", 0) > 86400 or len(u["coins"]) < 100:
        try:
            u = build()
        except Exception as e:
            print(f"universe: refresh failed ({type(e).__name__}), keeping {len(u['coins'])} coins")
    return u["coins"]


if __name__ == "__main__":
    c = refresh("--force" in sys.argv)
    u = load()["info"]
    print(len(c), "coins;", sum(1 for x in c if u[x]["spot"] and u[x]["perp"]), "both,",
          sum(1 for x in c if u[x]["spot"] and not u[x]["perp"]), "spot only,", sum(1 for x in c if u[x]["perp"] and not u[x]["spot"]), "perps only")
    print(c)
    print("min vol", min(u[x]["vol24h"] for x in c))
