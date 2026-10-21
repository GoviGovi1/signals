"""Build site/data.json from signals.json + backtest.json, then commit and push site/ to GitHub Pages
ONLY when the data changed. No secrets ever go into site/.
usage: python3 publish.py            (build + push if changed)
       python3 publish.py --no-push  (build only)"""
import hashlib, json, os, subprocess, sys
from datetime import datetime, timezone
import config as C

OUT = os.path.join(C.SITE_DIR, "data.json")
KEEP = ["id", "coin", "market", "side", "entry", "sl0", "sl", "tps", "lev", "funding_pct", "status", "hits",
        "opened", "closed_at", "exit_price", "result_r", "result_pct", "last", "last_ts", "move_pct", "closed", "rsi"]


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds") if ms else None


def clean(s):
    d = {k: s.get(k) for k in KEEP}
    d["opened"] = iso(s["opened"]); d["closed_at"] = iso(s.get("closed_at")); d["last_ts"] = iso(s.get("last_ts"))
    if s["market"] != "PERP":
        d["lev"] = None; d["funding_pct"] = None
    return d


def agg(ss):
    n = len(ss)
    if not n:
        return {"trades": 0}
    w = sum(1 for s in ss if s["result_r"] > 0)
    return {"trades": n, "wins": w, "losses": n - w, "win_rate": round(w / n * 100, 1),
            "total_r": round(sum(s["result_r"] for s in ss), 2), "avg_r": round(sum(s["result_r"] for s in ss) / n, 3),
            "total_pct": round(sum(s["result_pct"] for s in ss), 2)}


def build():
    sigs = json.load(open(C.SIGNALS_JSON)) if os.path.exists(C.SIGNALS_JSON) else []
    open_ = [clean(s) for s in sigs if not s["closed"]]
    closed = [s for s in sigs if s["closed"]]
    closed.sort(key=lambda s: s.get("closed_at") or 0, reverse=True)
    stats = {"all": agg(closed), "SPOT": agg([s for s in closed if s["market"] == "SPOT"]),
             "PERP": agg([s for s in closed if s["market"] == "PERP"]),
             "coins": {c: agg([s for s in closed if s["coin"] == c]) for c in C.COINS if any(s["coin"] == c for s in closed)},
             "open": len(open_)}
    bt = None
    if os.path.exists(C.BACKTEST_JSON):
        b = json.load(open(C.BACKTEST_JSON))
        bt = {"from": b["from"], "to": b["to"], "days": b["days"], "fees": b["fees_pct_round_trip"],
              **{k: b["summary"][k] for k in ["all", "SPOT", "PERP", "PERP_LONG", "PERP_SHORT"]},
              "coins": b["summary"]["coins"]}
        before = os.path.join(C.DIR, "backtest_before.json")
        if os.path.exists(before):
            bb = json.load(open(before))["summary"]
            bt["before"] = {k: bb[k] for k in ["all", "SPOT", "PERP"]}
    return {"open": open_, "closed": [clean(s) for s in closed[:60]], "stats": stats, "backtest": bt,
            "telegram": C.TG_LINK, "coins": C.COINS,
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
