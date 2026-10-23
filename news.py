"""Free crypto news layer (LIVE filter only: free feeds have no usable history, so it can't be backtested).
Sources (no keys): RSS from CoinDesk, Cointelegraph, The Block, Decrypt, CryptoSlate + OKX official
announcements (new listings, delistings). Headlines are matched to coins in the universe (ticker or name)
and flagged with simple keywords:
  bearish: hack, exploit, drained, delist, lawsuit/sued, SEC action, fraud, rug pull, halted withdrawals ...
  bullish: listing, partnership, ETF approval, mainnet, upgrade, integration ...
Used by run.py to (i) skip a LONG on fresh (24h) bearish news / a SHORT on fresh bullish news for that coin,
(ii) put the headline + link in the signal's "Why", (iii) send a rate-limited follow-up on open signals.
Cache: catalyst/news.json (refreshed at most every 10 min, ~1-3 s).  usage: python3 news.py [--force]"""
import json, re, sys, time, urllib.request, html
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
import catalyst as K, universe

UA = {"User-Agent": "Mozilla/5.0 (free-signals; +https://govigovi1.github.io/signals/)"}
RSS = {"CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
       "Cointelegraph": "https://cointelegraph.com/rss",
       "The Block": "https://www.theblock.co/rss.xml",
       "Decrypt": "https://decrypt.co/feed",
       "CryptoSlate": "https://cryptoslate.com/feed/"}
OKX_ANN = {"announcements-new-listings": "bullish", "announcements-delistings": "bearish"}
REFRESH_S, KEEP_S = 600, 3 * 86400
VETO_S = 24 * 3600              # fresh news window for the signal veto / Why line
FOLLOW_S = 6 * 3600             # follow-ups only for news at most 6 h old
MAX_FOLLOW_SIG, MAX_FOLLOW_DAY = 2, 6
NO_VETO = {"BTC", "ETH"}

BEAR = [r"hack(ed|er|ers)?", r"exploit(ed|s)?", r"drain(ed|s)?", r"breach", r"delist(s|ed|ing)?", r"lawsuit", r"\bsue[sd]?\b",
        r"fraud", r"rug ?pull", r"insolven", r"bankrupt", r"halt(s|ed)? withdrawals", r"suspend(s|ed)? withdrawals",
        r"wells notice", r"subpoena", r"charge[sd] by", r"probe", r"investigation", r"stolen", r"attacker"]
BULL = [r"\blist(s|ed|ing)\b", r"will list", r"partner(s|ship)", r"etf (approv|launch|debut)", r"approv(es|ed|al) .*etf",
        r"mainnet", r"upgrade", r"integrat(es|ion)", r"adopt(s|ion)", r"treasury (buy|purchase)", r"\bbuys?\b .*(million|billion)"]
APPROVE = re.compile(r"approv|green.?light|clears?\b", re.I)
SEC = re.compile(r"\bSEC\b")
BEAR_RE = re.compile("|".join(BEAR), re.I)
BULL_RE = re.compile("|".join(BULL), re.I)

# tickers that are also common words / too short: only matched as $SYM or (SYM)
AMBIG = {"ONE", "CORE", "DATA", "EDGE", "HOME", "MET", "CAP", "LAB", "SKY", "SENT", "BEAT", "GRAM", "NIGHT", "CHIP",
         "BILL", "DOS", "MEGA", "PROS", "ATH", "SOON", "TRUTH", "PEOPLE", "MAGIC", "SAND", "MASK", "BAT", "APE", "RIVER",
         "LIGHT", "BASED", "CARDS", "APR", "BIO", "BABY", "INIT", "SATS", "OPN", "ESP", "KAT", "MON", "USELESS", "GAS",
         "ICP", "CC", "CP", "OL", "UB", "RE", "LA", "AI", "OP", "AR", "ZK", "H", "W", "S", "A", "PI", "SPX", "TRIA", "ALLO",
         "HOME", "PHA", "AEON", "RAVE", "ENSO", "MMT", "CNPY", "SLX", "RLS", "ARX", "TRUMP", "HYPE", "PUMP", "DOOD"}
NAMES = {"BTC": ["bitcoin"], "ETH": ["ethereum", "ether"], "SOL": ["solana"], "XRP": ["ripple", "xrp ledger"],
         "DOGE": ["dogecoin"], "BNB": ["bnb chain", "binance coin"], "LINK": ["chainlink"], "AVAX": ["avalanche"],
         "ADA": ["cardano"], "DOT": ["polkadot"], "TRX": ["tron"], "LTC": ["litecoin"], "SHIB": ["shiba inu"],
         "HYPE": ["hyperliquid"], "TRUMP": ["trump memecoin", "trump coin", "$trump"], "PUMP": ["pump.fun"],
         "UNI": ["uniswap"], "AAVE": ["aave"], "ARB": ["arbitrum"], "OP": ["optimism"], "NEAR": ["near protocol"],
         "SUI": ["sui network", "sui blockchain"], "TON": ["toncoin"], "ZEC": ["zcash"], "XLM": ["stellar"],
         "WLD": ["worldcoin", "world network"], "ENA": ["ethena"], "ONDO": ["ondo finance"], "TIA": ["celestia"],
         "INJ": ["injective"], "APT": ["aptos"], "HBAR": ["hedera"], "ATOM": ["cosmos hub"], "FIL": ["filecoin"],
         "ETC": ["ethereum classic"], "BCH": ["bitcoin cash"], "TAO": ["bittensor"], "PENGU": ["pudgy penguins"],
         "WLFI": ["world liberty"], "ICP": ["internet computer"], "STRK": ["starknet"], "POL": ["polygon"],
         "LDO": ["lido"], "EIGEN": ["eigenlayer"], "PENDLE": ["pendle"], "MORPHO": ["morpho"], "ASTER": ["aster dex"],
         "XPL": ["plasma"], "S": ["sonic labs"], "ZK": ["zksync"], "AR": ["arweave"], "RENDER": ["render network"],
         "JTO": ["jito"], "PYTH": ["pyth network"], "SEI": ["sei network"], "KAITO": ["kaito"], "VIRTUAL": ["virtuals protocol"]}


def coin_patterns():
    pats = {}
    for c in universe.coins():
        alts = [rf"\${re.escape(c)}\b", rf"\({re.escape(c)}\)"]
        if len(c) >= 3 and c not in AMBIG and not c.isdigit():
            alts.append(rf"(?<![A-Za-z0-9$]){re.escape(c)}(?![A-Za-z0-9])")      # upper-case ticker as a word
        p = "|".join(alts)
        nm = NAMES.get(c, [])
        rx = [re.compile(p)] + ([re.compile(r"\b(" + "|".join(re.escape(n) for n in nm) + r")\b", re.I)] if nm else [])
        pats[c] = rx
    return pats


def classify(title):
    t = title
    if SEC.search(t) and not APPROVE.search(t):
        return "bearish", "SEC"
    m = BEAR_RE.search(t)
    if m:
        return "bearish", m.group(0).lower()
    m = BULL_RE.search(t)
    if m:
        return "bullish", m.group(0).lower()
    return None, None


def get(url, timeout=8):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()


def rss(src, url):
    out = []
    root = ET.fromstring(get(url))
    for it in root.iter("item"):
        title = html.unescape((it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip().split("?utm_")[0]
        try:
            ts = int(parsedate_to_datetime(it.findtext("pubDate")).timestamp())
        except Exception:
            continue
        if title and link:
            out.append({"title": title, "url": link, "ts": ts, "src": src})
    return out


def okx_ann(ann_type, tone):
    d = json.loads(get(f"https://www.okx.com/api/v5/support/announcements?annType={ann_type}"))
    out = []
    for blk in d.get("data", []):
        for x in blk.get("details", []):
            out.append({"title": x["title"], "url": x["url"], "ts": int(x["pTime"]) // 1000, "src": "OKX", "tone": tone})
    return out


def refresh(force=False):
    d = K.jload("news")
    now = time.time()
    if not force and now - d.get("updated", 0) < REFRESH_S:
        return None
    jobs = [(rss, (s, u), s) for s, u in RSS.items()] + [(okx_ann, (a, t), "OKX " + a.split("-")[-1]) for a, t in OKX_ANN.items()]
    items, failed = [], []
    with ThreadPoolExecutor(len(jobs)) as ex:
        futs = [(nm, ex.submit(fn, *a)) for fn, a, nm in jobs]
        for nm, f in futs:
            try:
                items += f.result(timeout=12)
            except Exception as e:
                failed.append(f"{nm}: {type(e).__name__}")
    pats = coin_patterns()
    old = {x["url"]: x for x in d.get("items", []) if now - x["ts"] < KEEP_S}
    for x in items:
        if now - x["ts"] > KEEP_S or x["url"] in old:
            continue
        if x["src"] == "OKX":
            tone = x.pop("tone"); why = "OKX listing" if tone == "bullish" else "OKX delisting"
        else:
            tone, why = classify(x["title"])
        coins = [c for c, rx in pats.items() if any(r.search(x["title"]) for r in rx)]
        if not coins:
            continue
        x.update(coins=coins, tone=tone, flag=why, id=re.sub(r"\W", "", x["url"])[-48:])
        old[x["url"]] = x
    keep = sorted(old.values(), key=lambda x: -x["ts"])[:400]
    K.jsave("news", {"updated": int(now), "items": keep, "sources": list(RSS) + ["OKX announcements"], "failed": failed})
    return len(items), len(keep), failed


def for_coin(coin, since):
    return [x for x in K.jload("news").get("items", []) if coin in x["coins"] and x["ts"] >= since]


def ago(ts):
    h = (time.time() - ts) / 3600
    return f"{h * 60:.0f} min ago" if h < 1 else f"{h:.0f}h ago"


def assess(coin, side):
    """-> (veto or None, reason line or None). side +1 long / -1 short. Fresh = last 24h."""
    xs = for_coin(coin, time.time() - VETO_S)
    if coin in NO_VETO:                         # BTC/ETH headlines are mostly market-wide: info only
        x = next((x for x in xs if x["tone"]), xs[0] if xs else None)
        return None, (f"News ({x['tone'] or 'neutral'}, {x['src']}, {ago(x['ts'])}): {x['title'][:100]} {x['url']}" if x else None)
    against = "bearish" if side > 0 else "bullish"
    bad = [x for x in xs if x["tone"] == against]
    if bad:
        x = bad[0]
        return f"fresh {against} news ({x['flag']}): {x['title'][:90]}", None
    good = [x for x in xs if x["tone"] and x["tone"] != against]
    x = (good or xs or [None])[0]
    if not x:
        return None, None
    tag = {"bullish": "bullish", "bearish": "bearish"}.get(x["tone"], "neutral")
    return None, f"News ({tag}, {x['src']}, {ago(x['ts'])}): {x['title'][:100]} {x['url']}"


def followups(sigs, now_ms):
    """Short Telegram follow-ups on open exchange signals when flagged news hits that coin (rate-limited)."""
    now = now_ms / 1000
    sent_today = sum(1 for s in sigs for n in s.get("news_sent", []) if now - n["at"] < 86400)
    out = []
    items = K.jload("news").get("items", [])
    for s in sigs:
        if s["closed"] or s["market"] == "DEX" or sent_today >= MAX_FOLLOW_DAY:
            continue
        done = s.setdefault("news_sent", [])
        if sum(1 for n in done if now - n["at"] < 86400) >= MAX_FOLLOW_SIG:
            continue
        side = 1 if s["side"] == "LONG" else -1
        for x in items:
            if s["coin"] not in x["coins"] or not x.get("tone") or x["ts"] < s["opened"] / 1000 or now - x["ts"] > FOLLOW_S:
                continue
            if any(n["id"] == x["id"] for n in done):
                continue
            against = (x["tone"] == "bearish") == (side > 0)
            tail = "consider tightening your stop." if against else "it supports your signal; keep the plan."
            out.append({"text": f"NEWS on {s['coin']} while your {s['market']} {s['side']} is open: {x['title']} ({x['src']}) "
                                f"{x['url']} — {x['tone']} news, {tail}"})
            done.append({"id": x["id"], "at": int(now), "tone": x["tone"]})
            sent_today += 1
            break
    return out


if __name__ == "__main__":
    print(refresh(force="--force" in sys.argv))
    for x in K.jload("news").get("items", [])[:40]:
        print(x["coins"], x["tone"], x["flag"], "|", x["title"][:100], "|", x["src"])
