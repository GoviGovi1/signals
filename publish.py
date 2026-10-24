"""Build site/data.json from signals.json + backtest.json, then commit and push site/ to GitHub Pages
ONLY when the data changed. No secrets ever go into site/.
usage: python3 publish.py            (build + push if changed)
       python3 publish.py --no-push  (build only)"""
import hashlib, json, os, subprocess, sys
from datetime import datetime, timezone
import config as C, universe

OUT = os.path.join(C.SITE_DIR, "data.json")
KEEP = ["id", "kind", "score", "reasons", "chain", "token", "url", "coin", "market", "side", "entry", "sl0", "sl", "tps", "lev", "funding_pct", "status", "hits",
        "opened", "closed_at", "exit_price", "result_r", "result_pct", "last", "last_ts", "move_pct", "closed", "rsi", "trade_on", "market_reason", "chart", "setup"]


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds") if ms else None


def clean(s):
    d = {k: s.get(k) for k in KEEP}
    d["kind"] = d["kind"] or "TA"
    d["opened"] = iso(s["opened"]); d["closed_at"] = iso(s.get("closed_at")); d["last_ts"] = iso(s.get("last_ts"))
    if s["market"] != "PERP":
        d["lev"] = None; d["funding_pct"] = None
    if s["market"] != "DEX":
        for k in ("chain", "token", "url"):
            d.pop(k, None)
    return d


def agg(ss):
    n = len(ss)
    if not n:
        return {"trades": 0}
    w = sum(1 for s in ss if s["result_r"] > 0)
    return {"trades": n, "wins": w, "losses": n - w, "win_rate": round(w / n * 100, 1),
            "total_r": round(sum(s["result_r"] for s in ss), 2), "avg_r": round(sum(s["result_r"] for s in ss) / n, 3),
            "total_pct": round(sum(s["result_pct"] for s in ss), 2)}


def cat(name):
    try:
        return json.load(open(os.path.join(C.DIR, "catalyst", name + ".json")))
    except (OSError, ValueError):
        return {}


def catalysts(coins):
    import time
    now = time.time()
    un, oc, sec, li, fu = cat("unlocks"), cat("onchain"), cat("sectors"), cat("listings"), cat("fundamentals")
    cal = [u for u in un.get("calendar", []) if now < u["ts"] <= now + 30 * 86400 and (u["big"] or u["pct"] >= 0.25)]
    recent = []
    for c, v in un.get("coins", {}).items():
        for r in v.get("recent", []):
            if r["pct"] >= 0.25 and v.get("tracked"):
                recent.append(dict(r, coin=c))
    recent.sort(key=lambda r: -r["ts"])
    names, ss = sec.get("names", {}), sec.get("sectors", {})
    sectors = [{"id": k, "name": names.get(k, k), "chg7d": ss[k]["chg7d"], "top": ss[k].get("top", []),
                "tracked": [c for c in coins if c in ss[k].get("members", [])][:8]} for k in sec.get("ranked", [])]
    movers = []
    for c, f in fu.get("coins", {}).items():
        for lab, k, size_k, min_size in (("TVL", "tvl_7d_pct", "tvl", 50e6), ("Chain TVL", "chain_tvl_7d_pct", "chain_tvl", 50e6),
                                         ("Fees", "fees_wow_pct", "fees_7d", 100e3), ("DEX volume", "dex_wow_pct", "dex_7d", 50e6)):
            if f.get(k) is not None and (f.get(size_k) or 0) >= min_size and abs(f[k]) >= 10:
                movers.append({"coin": c, "metric": lab, "chg7d": f[k], "size": f.get(size_k)})
    movers.sort(key=lambda m: -m["chg7d"])
    keep = ("chain", "symbol", "name", "token", "price", "liq", "vol24h", "age_d", "chg24h", "chg7d", "mcap",
            "safety", "safety_notes", "trigger", "trend_up", "rsi", "url")
    return {"unlocks": cal[:40], "unlocks_recent": recent[:10], "unlock_rule": un.get("rule"),
            "onchain": [{k: w.get(k) for k in keep} for w in oc.get("watchlist", [])],
            "onchain_checked": oc.get("checked"), "onchain_rule": oc.get("rule"),
            "sectors": sectors, "listings": li.get("new", [])[:15], "fund_movers": movers[:12],
            "updated": {k: v.get("updated") for k, v in (("unlocks", un), ("onchain", oc), ("sectors", sec),
                                                          ("listings", li), ("fundamentals", fu))}}


def build():
    sigs = json.load(open(C.SIGNALS_JSON)) if os.path.exists(C.SIGNALS_JSON) else []
    open_ = [clean(s) for s in sigs if not s["closed"]]
    closed = [s for s in sigs if s["closed"]]
    closed.sort(key=lambda s: s.get("closed_at") or 0, reverse=True)
    coins = universe.coins()
    stats = {"all": agg(closed), "SPOT": agg([s for s in closed if s["market"] == "SPOT"]),
             "PERP": agg([s for s in closed if s["market"] == "PERP"]),
             "DEX": agg([s for s in closed if s["market"] == "DEX"]),
             "kinds": {k: agg([s for s in closed if s.get("kind", "TA") == k]) for k in
                       sorted({s.get("kind", "TA") for s in closed})},
             "coins": {c: agg([s for s in closed if s["coin"] == c]) for c in sorted({s["coin"] for s in closed})},
             "open": len(open_)}
    bt = None
    if os.path.exists(C.BACKTEST_JSON):
        b = json.load(open(C.BACKTEST_JSON))
        bt = {"from": b["from"], "to": b["to"], "days": b["days"], "fees": b["fees_pct_round_trip"],
              **{k: b["summary"][k] for k in ["all", "SPOT", "PERP", "PERP_LONG", "PERP_SHORT"]},
              "coins": b["summary"]["coins"]}
        cb = os.path.join(C.DIR, "backtest_catalyst.json")
        if os.path.exists(cb):
            x = json.load(open(cb))
            bt["catalyst"] = {k: x[k] for k in ("from", "to", "mid", "coins", "variants", "catalyst", "data_coverage")}
            bt["catalyst"]["splits"] = x.get("splits", {})
            bt["catalyst"]["decisions"] = x.get("decisions", {})
            for k in bt["catalyst"]["catalyst"].values():
                k.pop("list", None)
        b200 = os.path.join(C.DIR, "backtest200.json")
        if os.path.exists(b200):
            bt["v3"] = json.load(open(b200))
        b4 = os.path.join(C.DIR, "backtest_v4.json")
        if os.path.exists(b4):
            bt["v4"] = json.load(open(b4))
        before = os.path.join(C.DIR, "backtest_before.json")
        if os.path.exists(before):
            bb = json.load(open(before))["summary"]
            bt["before"] = {k: bb[k] for k in ["all", "SPOT", "PERP"]}
    return {"open": open_, "closed": [clean(s) for s in closed[:60]], "stats": stats, "backtest": bt,
            "telegram": C.TG_LINK, "coins": coins, "catalysts": catalysts(coins),
            "filters": C.CATALYST_FILTERS, "v3_filters": getattr(C, "V3_FILTERS", {}),
            "caps": {"max_open": C.MAX_OPEN, "max_new_per_day": getattr(C, "MAX_NEW_PER_DAY", None),
                     "max_open_dex": C.MAX_OPEN_DEX, "max_new_dex_per_day": C.MAX_NEW_DEX_PER_DAY,
                     "max_hot_per_run": C.HOT["max_per_run"], "max_hot_per_day": C.HOT["max_per_day"],
                     "max_hot_open": C.HOT["max_open"]}, "catalyst_signals": C.CATALYST_SIGNALS,
            "rules": {k: C.P[k] for k in ["breakout_n", "atr_mult", "expire_h", "max_lev"]}}


def main():
    data = build()
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    old = None
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT)).get("digest")
        except ValueError:
            pass
    if old == digest:
        return
    data["digest"] = digest
    data["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = OUT + ".tmp"
    json.dump(data, open(tmp, "w"), indent=1)
    os.replace(tmp, OUT)
    if "--no-push" in sys.argv:
        print(f"publish: built site/data.json ({len(data['open'])} open), push skipped (--no-push)")
        return
    git = ["git", "-C", C.SITE_DIR]
    if not os.path.isdir(os.path.join(C.SITE_DIR, ".git")) or \
            subprocess.run(git + ["remote", "get-url", "origin"], capture_output=True).returncode != 0:
        print("publish: built site/data.json, NOT pushed (site/ has no GitHub remote)")
        return
    subprocess.run(git + ["add", "-A"], check=True)
    if subprocess.run(git + ["diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(git + ["commit", "-q", "-m", f"data {data['generated']}"], check=True)
    p = subprocess.run(git + ["push", "-q", "origin", "HEAD"], capture_output=True, text=True)
    if p.returncode != 0:
        print(f"publish: push FAILED: {p.stderr.strip()[:200]}")
        sys.exit(1)
    if "-v" in sys.argv:
        print(f"publish: pushed ({len(data['open'])} open, {data['stats']['all'].get('trades', 0)} closed)")


if __name__ == "__main__":
    main()
