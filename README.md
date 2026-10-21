# Free crypto Spot + Perps signals (signals only, paper-tracked)
No keys, no wallet, no orders. Data: OKX public candles + funding (works from GitHub's US runners).

Runs on **GitHub Actions** for free (`.github/workflows/signals.yml`, every 15 min + manual run):
1. `python3 state.py unpack`  decrypt private state (subscribers, Telegram offset, outbox) from `state/private.enc`
2. `python3 run.py`           new signals + status updates -> signals.json/csv and private/outbox.jsonl
3. `python3 bot.py --once`    getUpdates, answer /start /signals /stats /help /stop, broadcast new lines
4. `python3 publish.py --no-push`  rebuild site/data.json
5. `python3 state.py pack`, commit `[skip ci]`, then deploy `site/` to GitHub Pages when it changed

Secrets (Actions): `SIGNALS_TG_TOKEN` (@Mhosignalbot), `SIGNALS_STATE_KEY` (AES key for state/).
`private/` is never committed; `state/private.enc` is AES-256-CBC (openssl, PBKDF2) + HMAC.

Other tools: `python3 bot.py` (long-polling loop, don't run it while Actions is active), `python3 backtest.py`
(90-day backtest -> backtest.json; backtest_before.json = v1 pullback strategy).
Site: https://govigovi1.github.io/signals/
