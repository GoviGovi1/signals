"""Catalyst layer: free, keyless fundamental / on-chain / derivatives data on top of the chart signals.
Signals only: reads public data, never touches a wallet or exchange account.

Sources (all tested from GitHub's US runners and the box, see tools/probe_sources.py):
  DefiLlama datasets  token unlock schedules (emissionsIndex)          refreshed every 6 h
  DefiLlama API       protocol TVL, fees, DEX volume, chain TVL         refreshed every 6 h
  CoinGecko (free)    coin ids, sector (category) median 7d performance 1 sector per run, ids daily
  OKX public          open interest, funding, new listings              per signal / hourly
  GeckoTerminal       trending pools on Solana/Base/BSC/Ethereum/Arbitrum + hourly DEX candles   hourly
  GoPlus (free)       token safety (honeypot, taxes, mint/freeze authority)                     hourly
Slow-changing data is cached in catalyst/*.json (committed) so each 15-minute run stays fast and
inside every free rate limit. Any source that fails keeps its last good file; nothing crashes the run.
usage: python3 catalyst.py [--force]   refresh whatever is due and print a one-line summary"""
import json, math, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone
import config as C, okx, universe

CD = os.path.join(C.DIR, "catalyst")
UA = {"User-Agent": "Mozilla/5.0 (free-signals; +https://govigovi1.github.io/signals/)", "Accept": "application/json"}
DAY = 86400

# ---------- rule thresholds (explainable, set before the backtest; see backtest_catalyst.py) ----------
R = {
    "unlock_big_pct": 2.0,          # a cliff unlock of >= 2% of circulating supply ...
    "unlock_big_usd": 10e6,         # ... or >= $10M (and at least unlock_min_pct)
    "unlock_min_pct": 0.5,
    "unlock_window_d": 7,           # "upcoming" = next 7 days
    "post_unlock_d": (1, 5),        # post-unlock long: 1-5 days after the unlock, price held
    "oi_confirm_pct": 5.0,          # OI up >= 5% in 24h with the breakout = confirmation
    "oi_exhaust_pct": -10.0,        # OI down >= 10% in 24h while price moved our way = exhaustion
    "funding_crowded": 0.05,        # |funding| >= 0.05% per 8h, paid by our side = crowded
    "fund_rise_pct": 20.0,          # TVL / fees / DEX volume up >= 20% week over week ...
    "fund_price_lag_pct": 5.0,      # ... while price is up < 5% over the same 7 days
    "fund_min_tvl": 50e6, "fund_min_fees7d": 100e3, "fund_min_dex7d": 50e6,
    "onchain_min_liq": 250e3, "onchain_min_age_d": 7, "onchain_min_vol24h": 250e3,
}
NETS = {"solana": "Solana", "base": "Base", "bsc": "BSC", "eth": "Ethereum", "arbitrum": "Arbitrum"}
GOPLUS_CHAIN = {"base": "8453", "bsc": "56", "eth": "1", "arbitrum": "42161"}
SECTORS = ["layer-1", "layer-2", "meme-token", "artificial-intelligence", "ai-agents", "decentralized-finance-defi",
           "decentralized-exchange", "decentralized-perpetuals", "real-world-assets-rwa", "privacy-coins", "gaming",
           "oracle", "zero-knowledge-zk", "depin", "interoperability", "storage", "lending-borrowing",
           "exchange-based-tokens", "solana-meme-coins", "bitcoin-ecosystem"]
SKIP_BASE = {"SOL", "WSOL", "ETH", "WETH", "USDC", "USDT", "WBNB", "BNB", "CBBTC", "WBTC", "USDC.E", "DAI", "USD1",
             "USDE", "FDUSD", "JITOSOL", "MSOL", "STETH", "WSTETH", "BTCB", "EURC", "USDS", "PYUSD", "CBETH", "AUSD"}


# ---------- plumbing ----------
def http(url, timeout=30, tries=2):
    for k in range(tries):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout))
        except urllib.error.HTTPError as e:
            if e.code == 429 and k < tries - 1:
                time.sleep(4)
                continue
            raise
        except (OSError, ValueError):
            if k == tries - 1:
                raise
            time.sleep(2)


def path(name):
    return os.path.join(CD, name + ".json")


def jload(name, default=None):
    try:
        return json.load(open(path(name)))
    except (OSError, ValueError):
        return {} if default is None else default


def jsave(name, obj):
    os.makedirs(CD, exist_ok=True)
    tmp = path(name) + ".tmp"
    json.dump(obj, open(tmp, "w"), indent=1, sort_keys=True)
    os.replace(tmp, path(name))


def due(name, max_age):
    return time.time() - jload(name).get("updated", 0) > max_age


def pct(a, b):
    return (a / b - 1) * 100 if b else None


# ---------- CoinGecko ids (daily) ----------
def refresh_geckoids(coins):
    ids = {}
    for page in (1, 2):
        rows = http(f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}")
        for r in rows:
            s = r["symbol"].upper()
            if s not in ids:                         # highest market cap wins a shared symbol
                ids[s] = r["id"]
        time.sleep(2)
    old = jload("geckoids").get("ids", {})
    jsave("geckoids", {"updated": int(time.time()), "ids": {c: ids.get(c) or old.get(c) for c in coins}})


# ---------- token unlocks (DefiLlama emissions dataset) ----------
def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def unlock_entries():
    """Full DefiLlama emissions index (one 22 MB download), keyed by upper-case symbol -> entry (largest mcap)."""
    d = http("https://defillama-datasets.llama.fi/emissionsIndex", timeout=60)["data"]
    by_sym, by_gid = {}, {}
    for e in d:
        tp = e.get("tokenPrice") or [{}]
        s = (tp[0].get("symbol") or "").upper()
        if e.get("gecko_id"):
            by_gid[e["gecko_id"]] = e
        if s and (s not in by_sym or _num(e.get("mcap")) > _num(by_sym[s].get("mcap"))):
            by_sym[s] = e
    return by_sym, by_gid


def cliffs(e):
    """[(ts, tokens, [(recipient, category, amount)])] cliff unlock events of one token, oldest first."""
    by = {}
    for u in e.get("unlockEvents") or []:
        al = [(a.get("recipient"), a.get("category"), _num(a.get("amount"))) for a in u.get("cliffAllocations") or []]
        if sum(a[2] for a in al) > 0:
            by.setdefault(int(u["timestamp"]), []).extend(al)
    out = []
    for ts, al in sorted(by.items()):                 # unlocks within the same hour count as one event
        if out and ts - out[-1][0] <= 3600:
            out[-1][2].extend(al)
        else:
            out.append([ts, 0, list(al)])
    return [(ts, sum(a[2] for a in al), al) for ts, _, al in out]


def refresh_unlocks(coins, okx_spot):
    now = time.time()
    by_sym, by_gid = unlock_entries()
    gids = jload("geckoids").get("ids", {})
    out, cal = {}, []
    wanted = list(dict.fromkeys(list(coins) + sorted(okx_spot)))
    for c in wanted:
        e = by_gid.get(gids.get(c) or "") or by_sym.get(c)
        if not e:
            continue
        circ = _num(e.get("circSupply"))
        price = _num((e.get("tokenPrice") or [{}])[0].get("price"))
        if circ <= 0:
            continue
        up, rec = [], []
        for ts, tok, al in cliffs(e):
            if now - 14 * DAY <= ts <= now + 30 * DAY:
                al.sort(key=lambda a: -a[2])
                ins = sum(a[2] for a in al if (a[1] or "") in ("insiders", "privateSale"))
                row = {"ts": ts, "tokens": round(tok), "pct": round(tok / circ * 100, 2), "usd": round(tok * price),
                       "insider_pct": round(ins / tok * 100) if tok else 0,
                       "who": [f"{a[0]}" for a in al[:3]]}
                (up if ts > now else rec).append(row)
        lin = _num(e.get("unlocksPerDay"))
        if not up and not rec and not lin:
            continue
        info = {"name": e.get("name"), "gecko_id": e.get("gecko_id"), "circ": round(circ), "price": price,
                "mcap": round(_num(e.get("mcap"))), "linear_week_pct": round(lin * 7 / circ * 100, 2),
                "upcoming": up, "recent": rec, "tracked": c in coins}
        if c in coins or any(is_big(u) for u in up if u["ts"] < now + R["unlock_window_d"] * DAY):
            out[c] = info
        for u in up:
            if is_big(u) or (c in coins and u["pct"] >= 0.25):
                cal.append(dict(u, coin=c, big=is_big(u), tracked=c in coins))
    cal.sort(key=lambda u: u["ts"])
    jsave("unlocks", {"updated": int(now), "coins": out, "calendar": cal[:80],
                      "source": "DefiLlama emissions dataset (free)",
                      "rule": f"big = cliff unlock >= {R['unlock_big_pct']}% of circulating supply, or >= $10M and >= {R['unlock_min_pct']}%"})


def is_big(u):
    return u["pct"] >= R["unlock_big_pct"] or (u["usd"] >= R["unlock_big_usd"] and u["pct"] >= R["unlock_min_pct"])


def unlock_view(coin, now=None):
    """What the unlock calendar says about one coin right now."""
    now = now or time.time()
    info = jload("unlocks").get("coins", {}).get(coin)
    if not info:
        return {}
    w = R["unlock_window_d"] * DAY
    nxt = [u for u in info["upcoming"] if now < u["ts"] <= now + w]
    big = [u for u in nxt if is_big(u)]
    lo, hi = R["post_unlock_d"]
    past = [u for u in info["recent"] if now - hi * DAY <= u["ts"] <= now - lo * DAY and is_big(u)]
    v = {"next7_pct": round(sum(u["pct"] for u in nxt), 2), "next7_usd": sum(u["usd"] for u in nxt),
         "big_next7": bool(big), "first_ts": nxt[0]["ts"] if nxt else None, "recent_big": past[-1] if past else None,
         "linear_week_pct": info.get("linear_week_pct", 0)}
    return v


# ---------- fundamentals (DefiLlama TVL, fees, DEX volume) ----------
CHAIN_OF = {"ETH": "Ethereum", "SOL": "Solana", "BNB": "BSC", "SUI": "Sui", "AVAX": "Avalanche", "NEAR": "Near",
            "ADA": "Cardano", "ARB": "Arbitrum", "STRK": "Starknet", "INJ": "Injective", "HYPE": "Hyperliquid L1",
            "XPL": "Plasma", "OKB": "X Layer", "TRX": "Tron", "APT": "Aptos", "SEI": "Sei", "TON": "TON",
            "OP": "OP Mainnet", "POL": "Polygon", "S": "Sonic", "BERA": "Berachain", "MNT": "Mantle", "XRP": "XRPL",
            "BTC": "Bitcoin", "LTC": "Litecoin", "DOGE": "Dogecoin", "TIA": None}


def refresh_fundamentals(coins):
    now = time.time()
    protos = http("https://api.llama.fi/protocols", timeout=60)
    gids = jload("geckoids").get("ids", {})
    sym_of_id, sym_of_parent, tvl = {}, {}, {}
    for p in protos:
        s = (p.get("symbol") or "").upper()
        if s in ("", "-") or p.get("category") in ("CEX", "Chain", "Bridge", "Canonical Bridge", "Cross Chain Bridge"):
            continue
        if p.get("gecko_id") and gids.get(s) and p["gecko_id"] != gids[s]:
            continue                                         # same ticker, different token
        sym_of_id[str(p["id"])] = s
        if p.get("parentProtocol"):
            sym_of_parent[p["parentProtocol"]] = s
        if s in coins and p.get("tvl") and s not in CHAIN_OF:     # gas tokens: chain TVL below instead
            t, ch = _num(p["tvl"]), p.get("change_7d")
            a = tvl.setdefault(s, [0.0, 0.0, []])
            a[0] += t
            a[1] += t / (1 + ch / 100) if ch is not None and ch > -99 else t
            a[2].append(p["name"])
    out = {}
    for c, (t, t7, names) in tvl.items():
        out.setdefault(c, {})["tvl"] = round(t)
        out[c]["tvl_7d_pct"] = round(pct(t, t7), 1) if t7 else None
        out[c]["name"] = names[0].split(" V")[0]
    for kind, url in (("fees", "https://api.llama.fi/overview/fees?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true&dataType=dailyFees"),
                      ("dex", "https://api.llama.fi/overview/dexs?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true")):
        agg = {}
        for p in http(url, timeout=60).get("protocols", []):
            did = str(p.get("defillamaId") or p.get("id") or "")
            par = p.get("parentProtocol") or ""
            if p.get("protocolType") == "chain":
                continue
            s = sym_of_id.get(did) or sym_of_parent.get(par)
            if s in coins:
                a = agg.setdefault(s, [0.0, 0.0, set()])
                a[0] += _num(p.get("total7d")); a[1] += _num(p.get("total14dto7d"))
                a[2].add(par.replace("parent#", "") if par else (p.get("slug") or ""))
        for c, (w, w0, slugs) in agg.items():
            out.setdefault(c, {})[f"{kind}_7d"] = round(w)
            out[c][f"{kind}_wow_pct"] = round(pct(w, w0), 1) if w0 > 0 else None
            out[c][f"{kind}_slugs"] = sorted(x for x in slugs if x)
    # chain-level TVL for coins that are a chain's gas token
    for c in coins:
        ch = CHAIN_OF.get(c)
        if not ch:
            continue
        try:
            h = http(f"https://api.llama.fi/v2/historicalChainTvl/{urllib.request.quote(ch)}")
        except Exception:
            continue
        if len(h) > 8 and h[-1]["tvl"] > 0:
            d = out.setdefault(c, {})
            d["chain"] = ch
            d["chain_tvl"] = round(h[-1]["tvl"])
            ch7 = pct(h[-1]["tvl"], h[-8]["tvl"])
            d["chain_tvl_7d_pct"] = round(ch7, 1) if abs(ch7) <= 100 else None   # >100% in a week = data jump
        time.sleep(0.2)
    jsave("fundamentals", {"updated": int(now), "coins": out, "source": "DefiLlama (free API)"})


def fund_view(coin, price_7d_pct):
    """Rising fundamentals while price lags -> list of (reason, strong?)"""
    f = jload("fundamentals").get("coins", {}).get(coin) or {}
    rs, strong = [], False
    checks = [("TVL", f.get("tvl_7d_pct"), (f.get("tvl") or 0) >= R["fund_min_tvl"]),
              ("Chain TVL", f.get("chain_tvl_7d_pct"), (f.get("chain_tvl") or 0) >= R["fund_min_tvl"]),
              ("Fees", f.get("fees_wow_pct"), (f.get("fees_7d") or 0) >= R["fund_min_fees7d"]),
              ("DEX volume", f.get("dex_wow_pct"), (f.get("dex_7d") or 0) >= R["fund_min_dex7d"])]
    for name, ch, big_enough in checks:
        if ch is None or not big_enough:
            continue
        if ch >= R["fund_rise_pct"]:
            rs.append(f"{name} {ch:+.0f}% 7d")
            if price_7d_pct is not None and price_7d_pct < R["fund_price_lag_pct"]:
                strong = True
        elif ch <= -R["fund_rise_pct"]:
            rs.append(f"{name} {ch:+.0f}% 7d")
    if strong and price_7d_pct is not None:
        rs.append(f"price only {price_7d_pct:+.0f}% 7d")
    return {"reasons": rs, "momentum": strong, "data": f}


# ---------- sectors (CoinGecko categories, one per run) ----------
def refresh_sector_one():
    d = jload("sectors")
    sec = d.get("sectors", {})
    todo = sorted(SECTORS, key=lambda s: sec.get(s, {}).get("ts", 0))[0]
    rows = http(f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&category={todo}&order=market_cap_desc"
                f"&per_page=50&price_change_percentage=24h,7d")
    ch = sorted(_num(r.get("price_change_percentage_7d_in_currency")) for r in rows
                if r.get("price_change_percentage_7d_in_currency") is not None)
    med = (ch[len(ch) // 2] if len(ch) % 2 else (ch[len(ch) // 2 - 1] + ch[len(ch) // 2]) / 2) if ch else None
    sec[todo] = {"ts": int(time.time()), "chg7d": round(med, 2) if med is not None else None, "n": len(rows),
                 "members": [r["symbol"].upper() for r in rows],
                 "top": [r["symbol"].upper() for r in rows[:5]]}
    names = d.get("names", {})
    names.setdefault(todo, todo.replace("-", " ").title().replace("Ai ", "AI ").replace("Defi", "DeFi").replace("Rwa", "RWA").replace("Zk", "ZK").replace("Depin", "DePIN"))
    ranked = sorted((k for k, v in sec.items() if v.get("chg7d") is not None and k in SECTORS), key=lambda k: -sec[k]["chg7d"])
    jsave("sectors", {"updated": int(time.time()), "sectors": sec, "names": names, "ranked": ranked,
                      "source": "CoinGecko categories (free), median 7d change of each sector's top 50 coins by market cap (robust to bad prices)"})


def sector_view(coin):
    """Best-ranked CoinGecko sector this coin belongs to (top 50 by market cap of that sector)."""
    d = jload("sectors")
    ranked, sec = d.get("ranked", []), d.get("sectors", {})
    n = len(ranked)
    mine = [k for k, s in enumerate(ranked) if coin in sec[s].get("members", [])]
    if not mine or n < 4:
        return {}
    k = mine[0]
    s = ranked[k]
    q = max(2, n // 4)
    tier = "leading" if k < q else "lagging" if k >= n - q else "middle"
    return {"sector": d.get("names", {}).get(s, s), "chg7d": sec[s]["chg7d"], "rank": k + 1, "of": n, "tier": tier}


# ---------- OKX new listings ----------
def refresh_listings():
    now = time.time() * 1000
    out = []
    for t in ("SPOT", "SWAP"):
        for x in okx.get("/api/v5/public/instruments", {"instType": t}):
            if not x["instId"].endswith(("-USDT", "-USDT-SWAP")):
                continue
            lt = int(x.get("listTime") or 0)
            if lt and now - 14 * DAY * 1000 <= lt <= now + 14 * DAY * 1000:
                out.append({"coin": x["instId"].split("-")[0], "market": "PERP" if t == "SWAP" else "SPOT",
                            "list_ts": lt // 1000, "state": x.get("state")})
    out.sort(key=lambda r: -r["list_ts"])
    jsave("listings", {"updated": int(time.time()), "new": out[:40],
                       "rule": "New on OKX: watch only. No signal until it has 60 days of history."})


# ---------- on-chain spot watchlist (GeckoTerminal + GoPlus) ----------
def _gt(url):
    time.sleep(2.2)                                   # GeckoTerminal free limit ~30 calls/min
    return http("https://api.geckoterminal.com/api/v2" + url)


def goplus(net, addr):
    """Return (ok, [problems]) or (None, ['not checked']) if GoPlus has no answer."""
    try:
        if net == "solana":
            r = http(f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={addr}")
            v = (r.get("result") or {}).get(addr)
            if not v:
                return None, ["safety not checked"]
            bad = []
            for k, lab in (("mintable", "mint authority on"), ("freezable", "freeze authority on"),
                           ("closable", "closable"), ("balance_mutable_authority", "balances can be changed")):
                if str((v.get(k) or {}).get("status", "0")) == "1":
                    bad.append(lab)
            if v.get("transfer_fee") not in ({}, None, "{}"):
                bad.append("transfer fee")
            if str(v.get("non_transferable", "0")) == "1":
                bad.append("non-transferable")
            return not bad, bad
        cid = GOPLUS_CHAIN[net]
        r = http(f"https://api.gopluslabs.io/api/v1/token_security/{cid}?contract_addresses={addr}")
        v = (r.get("result") or {}).get(addr.lower())
        if not v:
            return None, ["safety not checked"]
        bad = []
        for k, lab in (("is_honeypot", "honeypot"), ("cannot_sell_all", "can't sell all"), ("hidden_owner", "hidden owner"),
                       ("owner_change_balance", "owner can change balances"), ("selfdestruct", "self-destruct"),
                       ("transfer_pausable", "transfers can be paused"), ("is_blacklisted", "blacklist"),
                       ("can_take_back_ownership", "ownership can be reclaimed"), ("slippage_modifiable", "tax can be changed")):
            if str(v.get(k, "0")) == "1":
                bad.append(lab)
        for k in ("buy_tax", "sell_tax"):
            if _num(v.get(k)) > 0.05:
                bad.append(f"{k.replace('_', ' ')} {_num(v.get(k)) * 100:.0f}%")
        if str(v.get("is_open_source", "1")) == "0":
            bad.append("contract not verified")
        return not bad, bad
    except Exception:
        return None, ["safety check failed"]


def dex_bars(net, pool, limit=200):
    d = _gt(f"/networks/{net}/pools/{pool}/ohlcv/hour?limit={limit}&currency=usd")
    rows = d["data"]["attributes"]["ohlcv_list"]
    out = [{"ts": int(r[0]) * 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
            "v": float(r[5]), "done": True} for r in rows]
    out.sort(key=lambda b: b["ts"])
    if out and out[-1]["ts"] + 3600 * 1000 > time.time() * 1000:
        out[-1]["done"] = False
    return out


def refresh_onchain(max_detail=6):
    import strategy as S
    now = time.time()
    cands, seen, errors = [], set(), []
    for net in NETS:
        try:
            d = _gt(f"/networks/{net}/trending_pools?include=base_token&duration=24h")
        except Exception as e:
            errors.append(f"{net}: {type(e).__name__}")
            continue
        toks = {t["id"]: t["attributes"] for t in d.get("included", []) if t.get("type") == "token"}
        for p in d.get("data", []):
            a = p["attributes"]
            bid = p["relationships"]["base_token"]["data"]["id"]
            t = toks.get(bid, {})
            sym = (t.get("symbol") or a["name"].split(" /")[0]).upper()
            addr = t.get("address") or bid.split("_", 1)[-1]
            if sym in SKIP_BASE or (net, addr) in seen:
                continue
            seen.add((net, addr))
            created = a.get("pool_created_at")
            age_d = (now - datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()) / DAY if created else 0
            liq, vol = _num(a.get("reserve_in_usd")), _num((a.get("volume_usd") or {}).get("h24"))
            row = {"chain": NETS[net], "net": net, "symbol": sym, "name": t.get("name") or sym, "token": addr,
                   "pool": a["address"], "price": _num(a.get("base_token_price_usd")), "liq": round(liq),
                   "vol24h": round(vol), "age_d": round(age_d, 1),
                   "chg24h": _num((a.get("price_change_percentage") or {}).get("h24")),
                   "mcap": _num(a.get("market_cap_usd") or a.get("fdv_usd")),
                   "gecko_id": t.get("coingecko_coin_id"),
                   "url": f"https://www.geckoterminal.com/{net}/pools/{a['address']}"}
            fails = []
            if liq < R["onchain_min_liq"]: fails.append("liquidity under $250k")
            if age_d < R["onchain_min_age_d"]: fails.append("pool younger than 7 days")
            if vol < R["onchain_min_vol24h"]: fails.append("24h volume under $250k")
            row["fails"] = fails
            cands.append(row)
    passed = [r for r in cands if not r["fails"]]
    passed.sort(key=lambda r: -r["vol24h"])
    detailed = 0
    for r in passed:
        if detailed >= max_detail:
            r["fails"].append("not checked this hour")
            continue
        ok, bad = goplus(r["net"], r["token"])
        r["safety"] = "ok" if ok else ("unknown" if ok is None else "fail")
        r["safety_notes"] = bad
        if ok is False:
            r["fails"] += bad
            continue
        try:
            bars = dex_bars(r["net"], r["pool"])
            detailed += 1
        except Exception:
            r["fails"].append("no DEX candles")
            continue
        done = [b for b in bars if b["done"]]
        if len(done) < 60:
            r["fails"].append("under 60h of candles")
            continue
        cl = [b["c"] for b in done]
        e20, e50, rs, at = S.ema(cl, 20), S.ema(cl, 50), S.rsi(cl, 14), S.atr(done, 14)
        i = len(done) - 1
        hh = max(b["h"] for b in done[i - 20:i])
        r.update({"close": cl[i], "ema20": e20[i], "ema50": e50[i], "rsi": round(rs[i], 1), "atr": at[i],
                  "bar_ts": done[i]["ts"], "chg7d": round(pct(cl[i], cl[max(0, i - 168)]), 1)})
        r["trend_up"] = e20[i] > e50[i] and cl[i] > e50[i]
        r["trigger"] = r["trend_up"] and cl[i] > hh and 55 <= rs[i] <= 75 and at[i] / cl[i] < 0.08
    wl = [r for r in cands if not r["fails"]]
    wl.sort(key=lambda r: (not r.get("trigger"), not r.get("trend_up"), -r["vol24h"]))
    jsave("onchain", {"updated": int(now), "watchlist": wl[:15], "checked": len(cands),
                      "rejected": [{k: r[k] for k in ("chain", "symbol", "fails")} for r in cands if r["fails"]][:40],
                      "errors": errors,
                      "rule": "GeckoTerminal trending pools on Solana, Base, BSC, Ethereum, Arbitrum; liquidity > $250k, "
                              "pool older than 7 days, 24h volume > $250k, GoPlus safety (no honeypot, taxes <= 5%, "
                              "no mint/freeze authority on Solana). Signal when the 1h DEX chart breaks its 20-hour high "
                              "above EMA50 with RSI 55-75."})


# ---------- derivatives (OKX, for signal candidates only) ----------
def derivs(coin):
    """OI change over 24 h (4h bars) and funding normalised to 8 h. Returns {} when unavailable."""
    out = {}
    try:
        oi = [r for r in okx.oi_history(coin, "4H", 9) if r["ts"] + 4 * 3600 * 1000 <= time.time() * 1000]
        if len(oi) >= 7 and oi[-7]["oi_usd"] > 0:            # finished 4h bars only, same as the backtest
            out["oi_24h_pct"] = round(pct(oi[-1]["oi_usd"], oi[-7]["oi_usd"]), 1)
            out["oi_usd"] = round(oi[-1]["oi_usd"])
    except Exception:
        pass
    try:
        f, iv = okx.funding_now(coin)
        out["funding_pct"] = round(f * 100, 4)
        out["funding_8h_pct"] = round(f * 100 * 8 / iv, 4)
    except Exception:
        pass
    return out


def derivs_eval(side, d, price_24h_pct):
    """(confirm, skips, reasons). side +1 long / -1 short. Pure function, used by the backtest too.
    skips is a list of codes: 'oi_falling', 'exhaustion', 'crowded' (config.CATALYST_FILTERS decides which block)."""
    rs, confirm, skips = [], False, []
    oi, f8 = d.get("oi_24h_pct"), d.get("funding_8h_pct")
    if oi is not None:
        if oi >= R["oi_confirm_pct"]:
            confirm = True
            rs.append(f"OI {oi:+.0f}% 24h with breakout")
        elif oi <= R["oi_exhaust_pct"] and price_24h_pct is not None and side * price_24h_pct > 0:
            skips.append("exhaustion")
            rs.append(f"OI {oi:+.0f}% 24h after the move (exhaustion)")
        else:
            rs.append(f"OI {oi:+.0f}% 24h" + (" (falling)" if oi < 0 else ""))
        if oi < 0:
            skips.append("oi_falling")
    if f8 is not None:
        if side * f8 >= R["funding_crowded"]:
            skips.append("crowded")
            rs.append(f"funding {f8:+.3f}%/8h: {'longs' if side > 0 else 'shorts'} crowded")
        elif abs(f8) >= R["funding_crowded"]:
            rs.append(f"funding {f8:+.3f}%/8h: other side crowded")
    return confirm, skips, rs


def coin_gate():
    """{coin: net R over the last 45 days} from the daily walk-forward check, or None if missing/stale."""
    g = jload("coin_gate")
    if not g or time.time() - g.get("updated", 0) > 3 * DAY:
        return None
    return g


# ---------- refresh everything that is due ----------
def refresh(force=False, onchain=True):
    """Called once per run. Each source is independent; failures keep the last good file."""
    done, failed = [], []
    coins = universe.refresh(force)

    def job(name, max_age, fn):
        if force or due(name, max_age):
            t0 = time.time()
            try:
                fn(); done.append(f"{name} {time.time() - t0:.0f}s")
            except Exception as e:
                failed.append(f"{name}: {type(e).__name__} {str(e)[:60]}")

    job("geckoids", DAY, lambda: refresh_geckoids(coins))
    spot = set()
    if force or due("unlocks", 6 * 3600):
        try:
            spot = {x["instId"].split("-")[0] for x in okx.get("/api/v5/public/instruments", {"instType": "SPOT"})
                    if x["instId"].endswith("-USDT")}
        except Exception:
            pass
    job("unlocks", 6 * 3600, lambda: refresh_unlocks(coins, spot))
    job("fundamentals", 6 * 3600, lambda: refresh_fundamentals(coins))
    job("listings", 3600, refresh_listings)
    job("sectors", 15 * 60 - 60, refresh_sector_one)
    if onchain:
        job("onchain", 3600 - 120, refresh_onchain)
    return done, failed


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%b %d %H:%M UTC")


if __name__ == "__main__":
    d, f = refresh("--force" in sys.argv)
    print("catalyst: refreshed", ", ".join(d) or "nothing due", ("| FAILED " + "; ".join(f)) if f else "")
