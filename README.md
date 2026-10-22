# Free crypto Spot + Perps signals (signals only, paper-tracked)
No keys, no wallet, no orders. All data is free and keyless and works from GitHub's US runners
(`tools/probe_sources.py`, manual workflow `probe.yml`):
OKX candles / open interest / funding / instruments, DefiLlama (unlock schedules, TVL, fees, DEX volume),
CoinGecko (coin ids, sectors), GeckoTerminal (trending pools, DEX candles), GoPlus (token safety).
Blocked or paid (not used): Binance + Bybit (geo-blocked from US runners and the box), DefiLlama `api.llama.fi/emission*`
(paid; the free `defillama-datasets.llama.fi` copy is used), Tokenomist (key), CryptoRank (blocked).

Universe: ~40 coins with both an OKX USDT perp and spot pair (`universe.py`, daily). Catalyst layer: `catalyst.py`
(cache in `catalyst/*.json`, committed; unlocks + fundamentals every 6 h, listings + on-chain hourly, one sector per run).
Rules and which ones are filters vs info: `config.py` (CATALYST_FILTERS, COIN_GATE, CATALYST_SIGNALS), decided by
`backtest_catalyst.py` (90 days, both halves) -> `backtest_catalyst.json`. Daily coin gate: `daily.yml`.

Runs on **GitHub Actions** for free (`.github/workflows/signals.yml`, every 15 min + manual run):
1. `python3 state.py unpack`  decrypt private state (subscribers, Telegram offset, outbox) from `state/private.enc`
2. `python3 run.py`           new signals + status updates -> signals.json/csv and private/outbox.jsonl
3. `python3 bot.py --once`    getUpdates, answer /start /signals /unlocks /watchlist /stats /help /stop, broadcast new lines
4. `python3 publish.py --no-push`  rebuild site/data.json
5. `python3 state.py pack`, commit `[skip ci]`, then deploy `site/` to GitHub Pages when it changed

Secrets (Actions): `SIGNALS_TG_TOKEN` (@Mhosignalbot), `SIGNALS_STATE_KEY` (AES key for state/).
`private/` is never committed; `state/private.enc` is AES-256-CBC (openssl, PBKDF2) + HMAC.

Other tools: `python3 bot.py` (long-polling loop, don't run it while Actions is active), `python3 bt_fetch.py`
(history cache), `python3 backtest.py` (90-day chart backtest, all universe coins -> backtest.json; backtest_before.json =
v1 pullback strategy on 9 coins), `python3 backtest_catalyst.py [--gate]`, `python3 catalyst.py [--force]`.
Site: https://govigovi1.github.io/signals/
