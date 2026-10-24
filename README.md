# Free crypto Spot + Perps signals (signals only, paper-tracked)
No keys, no wallet, no orders. All data is free and keyless and works from GitHub's US runners
(`tools/probe_sources.py`, manual workflow `probe.yml`):
OKX candles / open interest / funding / instruments, DefiLlama (unlock schedules, TVL, fees, DEX volume),
CoinGecko (coin ids, sectors), GeckoTerminal (trending pools, DEX candles), GoPlus (token safety).
Blocked or paid (not used): Binance + Bybit (geo-blocked from US runners and the box), DefiLlama `api.llama.fi/emission*`
(paid; the free `defillama-datasets.llama.fi` copy is used), Tokenomist (key), CryptoRank (blocked).

Universe: up to 350 OKX crypto coins ranked by 24h volume, USDT spot and/or perp, $0.25M+ combined volume, chart market 30+ days old, no
stables/wrapped/leveraged tokens (`universe.py`, daily and `python3 universe.py --force`, `catalyst/universe.json`). The cap is not filled with thinner names: a forced rebuild for this change found 279 coins (every book that cleared the filters), including WET, which was outside the old top 200 despite about $21M of 24h volume and a market older than 300 days. The 9 majors stay in while they still clear the rules. Hysteresis keeps a coin that is already listed while it remains inside the top 450. 30 days covers the 200 hourly bars the chart scan needs; the 20-hour breakout's 4h EMA200 keeps reading "no trend" until ~34 days of 4h candles exist. The daily coin-gate workflow rebuilds the list again. Backtest files already in the repo were run on the earlier ~200-coin list and were not re-run for this widen.

Hot movers (`hot.py`, every `run.py`, not only the hourly chart scan): a long when an OKX USDT book is up at least 20% in 24h, that book traded at least $0.5M, the market is at least 2 days old, and volume is at least 1.8× its recent average (2.2× when only a 12h window exists). At most 2 new signals a run, 3 a UTC day, and 3 open at once. They skip the 45-day coin gate on purpose (a coin that just joined the chart list still waits for that gate before a chart signal) and do not take the chart strategy's daily slots. Stop is 2× the 1h ATR, clamped between 4% and 12%, targets at 1R/2R/3R, same exit weights, and the same spot-vs-perps choice. `python3 hot.py` prints the current candidates and sends nothing; `python3 hot.py --no-candles` stops after the ticker ranking. No new secrets and no worker redeploy: this still runs inside the existing Actions step.

Strategy v4 (`chartread.py`, decided by `backtest_v4.py` -> `backtest_v4.json`): support/resistance from swing
highs/lows + volume nodes, 1d/4h/1h trend and 4h structure, range/base detection; setups range breakout, retest,
break of structure, 20h breakout; structure stops/targets; skip if next level < 2R or ATR >= 2.5%/h (`config.V4`);
ML scorer tested walk-forward and kept off (did not beat the rules). Older v3 study: v2 chart rule +
filters from `config.V3_FILTERS` (kept only if they beat the base in both halves of `backtest200.py` -> `backtest200.json`),
caps MAX_OPEN / MAX_NEW_PER_DAY, OI veto, daily coin gate (`backtest_v4.py --gate`). `choose_market()` picks
"Trade on: Spot" or "Trade on: Perps Nx" with a reason; shorts perps only, leverage <= 3x and far from the stop.
`chart.py` renders a PNG per new signal (site/charts/, pruned; sent with Telegram sendPhoto, caption = signal text).
The full chart scan runs once per closed hour; other runs track open signals and still check hot movers. Catalyst layer: `catalyst.py`
(cache in `catalyst/*.json`, committed; unlocks + fundamentals every 6 h, listings + on-chain hourly, one sector per run).
Rules and which ones are filters vs info: `config.py` (CATALYST_FILTERS, COIN_GATE, CATALYST_SIGNALS), decided by
`backtest_catalyst.py` (90 days, both halves) -> `backtest_catalyst.json`. Daily coin gate: `daily.yml`.

Runs on **GitHub Actions** for free (`.github/workflows/signals.yml`, every 15 min, plus an external cron that
dispatches the same workflow about every 5 min):
1. `python3 state.py unpack`  decrypt private state (subscribers, Telegram offset, outbox) from `state/private.enc`
2. `python3 run.py`           new signals + status updates -> signals.json/csv and private/outbox.jsonl
3. `python3 bot.py --serve`   publish command replies, sync /start and /stop, broadcast new signals (photo + caption).
   Polls `getUpdates` only until the webhook below is configured. After that it does not poll.
4. `python3 publish.py --no-push`  rebuild site/data.json
5. `python3 state.py pack`, commit `[skip ci]`, then deploy `site/` to GitHub Pages when it changed

Secrets (Actions): `SIGNALS_TG_TOKEN` (@Mhosignalbot), `SIGNALS_STATE_KEY` (AES key for state/),
and, once the worker exists, `SIGNALS_WEBHOOK_URL` plus `SIGNALS_WEBHOOK_SECRET`.
`private/` is never committed; `state/private.enc` is AES-256-CBC (openssl, PBKDF2) + HMAC.

### Instant commands (Cloudflare Worker, free)
Telegram delivers `/start`, `/signals`, `/stats`, `/help`, `/stop`, `/unlocks`, and `/watchlist` to a
[Cloudflare Worker](worker/) webhook (`https://<name>.<account>.workers.dev/hook`) within a few seconds.
The worker does not scan markets and does not send the signal photos. Each Actions run renders the reply
text with the same `bot.py` functions as before, stores it for the worker, applies join/leave events onto
`private/subscribers.json`, then broadcasts. `getUpdates` and the webhook are never used together: while a
webhook is set, `--serve` only broadcasts, and `python3 bot.py` (the long-polling loop) exits instead of polling.

Free limits (no credit card, resets 00:00 UTC): Workers 100,000 requests/day and 10 ms of CPU per request
(waiting on Telegram or KV does not count). Workers KV: 100,000 reads, 1,000 writes, 1,000 deletes, and
1,000 list operations per day, 1 GB stored. This bot uses one write when the reply text changes and one list
per scan (~300/day at a 5-minute cadence), plus one write per `/start` or `/stop` and one read per command.
The worker does not sleep. Do not turn on the Workers Paid plan.

Deploy and rotation steps are in the pull request that added `worker/`. Short version: `npx wrangler kv namespace create SUBS`,
paste the id into `worker/wrangler.toml`, `npx wrangler deploy`, then `npx wrangler secret put TG_TOKEN` and
`npx wrangler secret put WEBHOOK_SECRET`. Put the `*.workers.dev` origin (no `/hook`) and the same secret in the two Actions secrets.
Confirm `https://<worker>/health` returns `{"ok":true}` before saving those secrets. To drop the webhook and go back to
polling, delete both Actions secrets and run the workflow with **webhook_off** checked.

Rotate the bot token: in @BotFather, revoke it, then update Actions secret `SIGNALS_TG_TOKEN` and
`npx wrangler secret put TG_TOKEN`, and run the signals workflow so `setWebhook` is called with the new token.
Rotate the webhook secret: generate a new one (`openssl rand -hex 32`), put it in `WEBHOOK_SECRET` and
`SIGNALS_WEBHOOK_SECRET`, and run the workflow. The secret is 1–256 characters of `A-Z a-z 0-9 _ -`.
Neither value is printed by the workflow.

Other tools: `python3 bot.py --once` (one poll, skipped automatically if a webhook is set), `python3 bot.py --push`
(broadcast only), `python3 bot.py --webhook-off`, `python3 bt_fetch.py`
(history cache), `python3 hot.py` (hot-mover dry run, sends nothing), `python3 backtest.py` (90-day chart backtest, all universe coins -> backtest.json; backtest_before.json =
v1 pullback strategy on 9 coins), `python3 backtest_catalyst.py [--gate]`, `python3 catalyst.py [--force]`.
Site: https://govigovi1.github.io/signals/
