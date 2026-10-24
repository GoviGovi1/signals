"""Coin universe: the ~350 most traded crypto coins on OKX (USDT spot pairs and/or USDT perpetuals).
Refreshed once a day into catalyst/universe.json (committed), so every run uses the same list.
Rules: crypto only (OKX instCategory 1: no tokenized stocks/commodities), no stablecoins, wrapped or
leveraged tokens; 24h USD volume (spot + perp) of at least $0.25M; the market we read the chart from must be
at least 30 days old. That is enough for the 200 closed 1h bars the chart scan requires. The 20-hour
breakout's 4h EMA200 stays quiet until ~34 days of 4h candles exist; range break, retest and break of
structure can fire before that. Younger listings are left to the hot-mover scan (hot.py, from day 2).
Ranked by volume; the original 9 majors stay in while they still pass the volume and age rules.
Hysteresis: a coin already in the list stays while it is in the top 450, so the list doesn't churn.
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
SIZE, KEEP_TOP, MIN_AGE_D, MIN_VOL = 350, 450, 30, 2.5e5
INFO_KEYS = ("coin", "spot", "perp", "vol_spot", "vol_perp", "age_spot", "age_perp", "vol24h", "series")


def excluded_symbol(c):
    """Stablecoins, wrapped tokens, leveraged tokens, and symbols that are not plain tickers."""
    return (not c) or c in STABLE or LEVERAGED.search(c) is not None or re.fullmatch(r"[A-Z0-9]{1,15}", c) is None


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


def rule_text():
    vol_m = MIN_VOL / 1e6
    vol_s = f"{vol_m:.2f}".rstrip("0").rstrip(".")
    return (f"up to {SIZE} crypto coins on OKX by 24h USD volume (spot + perp), "
            f">= ${vol_s}M/day, chart market >= {MIN_AGE_D} days old; "
            f"hysteresis keeps a current coin while it stays inside the top {KEEP_TOP}; "
            "no stablecoins, wrapped or leveraged tokens, no tokenized stocks")


def _vol_usd(ticker, inst_type):
    try:
        if inst_type == "SWAP":
            return float(ticker["volCcy24h"]) * float(ticker["last"])
        return float(ticker["volCcy24h"])
    except (TypeError, ValueError, KeyError):
        return 0.0


def _chg(ticker):
    """(24h percent from open24h to last, last price) or (None, 0)."""
    try:
        last = float(ticker["last"])
        op = float(ticker["open24h"])
        if op <= 0 or last <= 0:
            return None, 0.0
        return (last / op - 1.0) * 100.0, last
    except (TypeError, ValueError, KeyError):
        return None, 0.0


def markets():
    """Every live OKX crypto USDT spot/perp coin. One row per coin, including 24h change.
    Shared by the daily universe build and the hot-mover scan. Does not write universe.json."""
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
            if excluded_symbol(c):
                continue
            k = tick.get(iid)
            chg, last = _chg(k) if k else (None, 0.0)
            r = rows.setdefault(c, {"coin": c, "spot": False, "perp": False, "vol_spot": 0, "vol_perp": 0,
                                    "age_spot": 0, "age_perp": 0, "chg_spot": None, "chg_perp": None,
                                    "last_spot": 0.0, "last_perp": 0.0})
            m = "perp" if t == "SWAP" else "spot"
            r[m] = True
            r[f"vol_{m}"] = round(_vol_usd(k, t) if k else 0)
            r[f"age_{m}"] = round((now - int(x.get("listTime") or now * 1000) / 1000) / 86400)
            r[f"chg_{m}"] = None if chg is None else round(chg, 2)
            r[f"last_{m}"] = last
    return list(rows.values())


def qualify(rows):
    """Rows that can be charted: enough history on spot or perp, and enough combined volume."""
    ok = []
    for src in rows:
        r = {k: src.get(k) for k in ("coin", "spot", "perp", "vol_spot", "vol_perp", "age_spot", "age_perp")}
        r["vol_spot"] = r["vol_spot"] or 0
        r["vol_perp"] = r["vol_perp"] or 0
        r["age_spot"] = r["age_spot"] or 0
        r["age_perp"] = r["age_perp"] or 0
        r["vol24h"] = r["vol_spot"] + r["vol_perp"]
        # read the chart from the perp when it is old enough, else from spot
        r["series"] = "PERP" if r["perp"] and r["age_perp"] >= MIN_AGE_D else "SPOT" if r["spot"] and r["age_spot"] >= MIN_AGE_D else None
        if r["series"] and r["vol24h"] >= MIN_VOL:
            ok.append(r)
    ok.sort(key=lambda r: -r["vol24h"])
    return ok


def select(ok, old, size=None, keep_top=None):
    """Ranked coin list. Majors first, then coins already listed while they stay inside keep_top, then by volume."""
    size = SIZE if size is None else size
    keep_top = KEEP_TOP if keep_top is None else keep_top
    rank = {r["coin"]: k for k, r in enumerate(ok)}
    missing = 10 ** 6
    out = list(dict.fromkeys([c for c in CORE if c in rank] + [c for c in old if rank.get(c, missing) < keep_top]))
    for r in ok:
        if len(out) >= size:
            break
        if r["coin"] not in out:
            out.append(r["coin"])
    while len(out) > size:
        rest = [c for c in out if c not in CORE]
        if not rest:
            break
        out.remove(max(rest, key=lambda c: rank.get(c, missing)))
    out.sort(key=lambda c: (c not in CORE, rank.get(c, missing)))
    return out


def build():
    now = time.time()
    ok = qualify(markets())
    old = load()["coins"]
    out = select(ok, old)
    info = {r["coin"]: {k: r[k] for k in INFO_KEYS} for r in ok}
    data = {"updated": int(now), "coins": out, "info": {c: info[c] for c in out if c in info}, "rule": rule_text()}
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
    print(rule_text())
    print(c)
    print("min vol", min(u[x]["vol24h"] for x in c))
