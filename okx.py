"""Free public OKX market data (no API key). Binance and Bybit are geo-blocked from this box."""
import json, time, urllib.request, urllib.parse

BASE = "https://www.okx.com"
BAR = {"1h": "1H", "4h": "4H", "1H": "1H", "4H": "4H"}


def get(path, params, tries=3):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "signals/1.0"})
            d = json.load(urllib.request.urlopen(req, timeout=15))
            if d.get("code") == "0":
                return d["data"]
            if d.get("code") == "50011":          # rate limit
                time.sleep(1.0 + k)
                continue
            raise RuntimeError(f"okx {path} code {d.get('code')} {d.get('msg')}")
        except (OSError, ValueError) as e:
            if k == tries - 1:
                raise
            time.sleep(1.0 + k)
    raise RuntimeError(f"okx {path} failed")


def inst(coin, market):
    return f"{coin}-USDT-SWAP" if market == "PERP" else f"{coin}-USDT"


def _rows(data, confirmed_only):
    out = []
    for r in data:
        if confirmed_only and r[8] != "1":
            continue
        out.append({"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                    "c": float(r[4]), "v": float(r[5]), "done": r[8] == "1"})
    return out


def recent(coin, market, bar, limit=300, confirmed_only=False):
    """Most recent candles, oldest first (includes the still-forming bar unless confirmed_only)."""
    d = get("/api/v5/market/candles", {"instId": inst(coin, market), "bar": BAR[bar], "limit": limit})
    return _rows(d, confirmed_only)[::-1]


def history(coin, market, bar, n):
    """Last n confirmed candles, oldest first (paginates history-candles, 100 per call)."""
    out, after = [], None
    while len(out) < n:
        p = {"instId": inst(coin, market), "bar": BAR[bar], "limit": 100}
        if after:
            p["after"] = after
        d = get("/api/v5/market/history-candles", p)
        if not d:
            break
        out.extend(_rows(d, True))
        after = d[-1][0]
        time.sleep(0.12)
    out = {r["ts"]: r for r in out}
    return [out[k] for k in sorted(out)][-n:]


def funding(coin):
    d = get("/api/v5/public/funding-rate", {"instId": inst(coin, "PERP")})
    return float(d[0]["fundingRate"]) if d else None


def last_price(coin, market):
    d = get("/api/v5/market/ticker", {"instId": inst(coin, market)})
    return float(d[0]["last"])
