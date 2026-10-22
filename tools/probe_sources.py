"""Probe free keyless data sources. Prints status, size, latency and a short shape hint. No secrets."""
import json, time, urllib.request, sys
UA = {"User-Agent": "Mozilla/5.0 (signals-probe)", "Accept": "application/json"}
T = [
 ("llama_emissions_list", "https://api.llama.fi/emissions"),
 ("llama_emissions_ds", "https://defillama-datasets.llama.fi/emissionsProtocolsList"),
 ("llama_emissions_idx", "https://defillama-datasets.llama.fi/emissionsIndex"),
 ("llama_emission_arb", "https://api.llama.fi/emission/arbitrum"),
 ("llama_emission_ds_arb", "https://defillama-datasets.llama.fi/emissions/arbitrum"),
 ("llama_protocols", "https://api.llama.fi/protocols"),
 ("llama_protocol_aave", "https://api.llama.fi/protocol/aave"),
 ("llama_chains", "https://api.llama.fi/v2/chains"),
 ("llama_hist_chain_tvl", "https://api.llama.fi/v2/historicalChainTvl/Solana"),
 ("llama_fees", "https://api.llama.fi/overview/fees?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"),
 ("llama_dexs", "https://api.llama.fi/overview/dexs?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"),
 ("llama_fees_proto", "https://api.llama.fi/summary/fees/uniswap?dataType=dailyFees"),
 ("llama_stables", "https://stablecoins.llama.fi/stablecoinchains"),
 ("llama_coins_price", "https://coins.llama.fi/prices/current/coingecko:bitcoin"),
 ("cg_trending", "https://api.coingecko.com/api/v3/search/trending"),
 ("cg_categories", "https://api.coingecko.com/api/v3/coins/categories"),
 ("cg_markets", "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&per_page=250&page=1"),
 ("okx_oi_vol", "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?ccy=BTC&period=1H"),
 ("okx_oi_hist", "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history?instId=BTC-USDT-SWAP&period=1H&limit=100"),
 ("okx_ls_ratio", "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy=BTC&period=1H"),
 ("okx_ls_ratio_inst", "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio-contract?instId=BTC-USDT-SWAP&period=1H&limit=100"),
 ("okx_funding_hist", "https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=100"),
 ("okx_oi_now", "https://www.okx.com/api/v5/public/open-interest?instType=SWAP"),
 ("okx_instruments", "https://www.okx.com/api/v5/public/instruments?instType=SPOT"),
 ("okx_tickers_swap", "https://www.okx.com/api/v5/market/tickers?instType=SWAP"),
 ("ds_boosts_top", "https://api.dexscreener.com/token-boosts/top/v1"),
 ("ds_profiles", "https://api.dexscreener.com/token-profiles/latest/v1"),
 ("ds_tokens", "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112"),
 ("gt_trending_sol", "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"),
 ("gt_trending_all", "https://api.geckoterminal.com/api/v2/networks/trending_pools"),
 ("gt_ohlcv", "https://api.geckoterminal.com/api/v2/networks/eth/pools/0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640/ohlcv/hour?limit=100"),
 ("goplus_bsc", "https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses=0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82"),
 ("goplus_sol", "https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses=JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"),
 ("honeypot_is", "https://api.honeypot.is/v2/IsHoneypot?address=0x6982508145454Ce325dDbE47a25d4ec3d2311933"),
 ("tokenomist_api", "https://api.tokenomist.ai/v1/token/list"),
 ("cryptorank", "https://api.cryptorank.io/v0/token-unlock"),
 ("cg_markets_cat7d", "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&category=layer-1&per_page=50&price_change_percentage=7d"),
 ("gt_trending_base", "https://api.geckoterminal.com/api/v2/networks/base/trending_pools"),
 ("gt_trending_bsc", "https://api.geckoterminal.com/api/v2/networks/bsc/trending_pools"),
 ("gt_trending_arb", "https://api.geckoterminal.com/api/v2/networks/arbitrum/trending_pools"),
 ("okx_oi_hist_1H_deep", "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history?instId=ETH-USDT-SWAP&period=1H&limit=100&end=1786000000000"),
 ("binance_oi", "https://fapi.binance.com/futures/data/openInterestHist?symbol=BTCUSDT&period=1h&limit=10"),
 ("bybit_oi", "https://api.bybit.com/v5/market/open-interest?category=linear&symbol=BTCUSDT&intervalTime=1h&limit=5"),
]
out = {}
only = sys.argv[1:]
for name, url in T:
    if only and name not in only: continue
    t0 = time.time()
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25)
        b = r.read(); st = r.status
    except urllib.error.HTTPError as e:
        b = e.read()[:300]; st = e.code
    except Exception as e:
        b = str(e).encode()[:200]; st = "ERR"
    dt = round(time.time() - t0, 2)
    hint = ""
    try:
        j = json.loads(b)
        if isinstance(j, list): hint = f"list[{len(j)}] " + (str(list(j[0].keys())[:12]) if j and isinstance(j[0], dict) else str(j[:1])[:150])
        elif isinstance(j, dict): hint = "dict " + str(list(j.keys())[:12]) + " " + (str(j.get("code","")) + str(j.get("msg",""))[:80] if "code" in j else "")
    except Exception:
        hint = b[:150].decode("utf8", "replace").replace("\n", " ")
    print(f"{name:24} {st} {len(b):>9}B {dt:>6}s {hint[:230]}")
    time.sleep(0.4)
