"""Download history for the backtests (cache/, not committed): 1h+4h candles of each coin's chart series, 4h open interest
and funding for every universe coin.
usage: python3 bt_fetch.py [--days N] [--no-funding]   (default 90 days; the daily coin gate uses 45)"""
import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
import config as C, okx, universe, backtest as B

os.makedirs(C.CACHE_DIR, exist_ok=True)
if "--days" in sys.argv:
    B.DAYS = int(sys.argv[sys.argv.index("--days") + 1])


def cached(name, fn, max_age=12 * 3600):
    path = os.path.join(C.CACHE_DIR, name)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age:
        return json.load(open(path))
    rows = fn()
    json.dump(rows, open(path, "w"))
    return rows


B.MAX_AGE = 20 * 3600


def one(c):
    inf = universe.info(c)
    for m in [inf.get("series") or "PERP"]:            # one chart series per coin (perp if old enough, else spot)
        try:
            B.load(c, m, "1h", B.DAYS * 24 + 300, False)
            B.load(c, m, "4h", B.DAYS * 6 + 260, False)
        except Exception as e:
            print("candles fail", c, m, type(e).__name__, flush=True)
    if not inf.get("perp"):
        return c
    try:
        cached(f"{c}_oi4h.json", lambda: okx.oi_history(c, "4H", 6 * (B.DAYS + 10)))
        if "--no-funding" not in sys.argv:
            cached(f"{c}_funding.json", lambda: okx.funding_history(c, 2000))
    except Exception as e:
        print("deriv fail", c, type(e).__name__, flush=True)
    return c


if __name__ == "__main__":
    t0 = time.time()
    with ThreadPoolExecutor(int(os.environ.get('BT_THREADS', '3'))) as ex:
        n = len(list(ex.map(one, universe.coins())))
    print(f"bt_fetch: {n} coins, {B.DAYS} days, {time.time() - t0:.0f}s")
