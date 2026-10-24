"""Settings for the free spot + perps signal engine. Signals only: no keys, no wallet, no orders."""
import os
DIR = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(DIR, "site")
PRIVATE_DIR = os.path.join(DIR, "private")          # never published (subscribers, bot state)
CACHE_DIR = os.path.join(DIR, "cache")
SIGNALS_JSON = os.path.join(DIR, "signals.json")
SIGNALS_CSV = os.path.join(DIR, "signals.csv")
OUTBOX = os.path.join(PRIVATE_DIR, "outbox.jsonl")   # run.py -> bot.py messages
BACKTEST_JSON = os.path.join(DIR, "backtest.json")

SITE_URL = "https://govigovi1.github.io/signals/"
TG_BOT_USERNAME = "Mhosignalbot"
TG_LINK = f"https://t.me/{TG_BOT_USERNAME}"

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "SUI", "AVAX", "LINK"]
MARKETS = ["SPOT", "PERP"]

# --- strategy (same params used by backtest.py and run.py) ---
P = {
    "ema_fast": 20, "ema_slow": 50,          # 1h (EMA50 must agree with the breakout)
    "breakout_n": 20,                         # close beyond the last 20 hourly highs/lows
    "trend_fast": 50, "trend_slow": 200,     # 4h trend filter
    "rsi_len": 14, "atr_len": 14,
    "rsi_long": (55, 75), "rsi_short": (25, 45),
    "atr_mult": 2.0,                          # stop distance = 2 x ATR(14, 1h)
    "expire_h": 48,
    "max_lev": 3,                             # perps leverage cap
    "lev_target_loss": 0.08,                  # leverage chosen so a stop costs ~8% of margin
}
FEES_PCT = {"SPOT": 0.2, "PERP": 0.1}         # round-trip taker fees used in backtest net numbers

# --- catalyst layer (see catalyst.py, backtest_catalyst.py) ---
MAX_OPEN = 15                 # total open exchange signals (spot + perps)
MAX_OPEN_DEX = 4              # open on-chain (DEX) watchlist signals
MAX_NEW_PER_DAY = 8           # new exchange signals per UTC day (best relative strength first)
MAX_NEW_DEX_PER_DAY = 2
# A catalyst rule becomes a FILTER only if it improved the TA signals in BOTH halves of the 90-day backtest
# (backtest_catalyst.py, 40 coins, 2026-07-24 -> 2026-10-22). Everything else is shown as a reason only.
CATALYST_FILTERS = {
    "oi_falling": True,          # skip when open interest fell over the last 24h (helped in both halves)
    "exhaustion": True,          # OI -10% after a move our way (subset of oi_falling; worst trades in both halves)
    "unlock_long_veto": False,   # big unlock within 7 days: mixed (helped H2, hurt H1) -> score/reason only
    "crowded": False,            # crowded funding: only 1 signal in 90 days -> reason only
    "require_oi_confirm": False, # OI +5%: helped H1, hurt H2 -> reason/score only
}
# Coin gate (walk-forward): chart signals only on coins where the same rules made money (after fees) over the
# previous 45 days. Re-checked daily by .github/workflows/daily.yml -> catalyst/coin_gate.json.
COIN_GATE = True
# Stand-alone catalyst signal types. All three lost money in the backtest, so they are off (display only).
# ONCHAIN can't be backtested with free data: on, capped, and labelled untested.
CATALYST_SIGNALS = {"UNLOCK_SHORT": False, "POST_UNLOCK_LONG": False, "FUND_LONG": False, "ONCHAIN": True}
# Hot movers (hot.py): a parallel scan, every run, not backtested. Long only.
# A coin up hard in 24h with a volume spike can signal even when it is outside the chart universe,
# or when the chart rules skip it (volatility, or no 45-day coin-gate record). Own caps, so it does
# not use up MAX_OPEN / MAX_NEW_PER_DAY.
HOT = {
    "on": True,
    "min_chg_pct": 20.0,          # OKX open24h -> last, on the book that moved
    "min_vol_usd": 5e5,           # that same book, 24h USD volume
    "min_age_d": 2,               # enough hourly bars for ATR and a chart
    "min_bars": 30,
    "spike_mult": 1.8,            # last 24h vs the recent daily average
    "spike_mult_short": 2.2,      # stricter when only a 12h window exists
    "stop_atr": 2.0,
    "stop_min_pct": 0.04,         # stop never tighter than 4% on a fast coin
    "stop_max_pct": 0.12,         # and never wider than 12%
    "max_per_run": 2,
    "max_per_day": 3,
    "max_open": 3,
    "fetch_cap": 8,               # 1h candle fetches per run, strongest ticker scores first
}

# Strategy v3 extra rules (decided by backtest200.py on the 200-coin universe; True = filter is on)
V3_FILTERS = {"BTC regime": False, "relative strength vs BTC": False, "volume 1.5x": False,
              "ATR% < 2.5 (calmer coins)": True}
# Strategy v4 (chartread.py), decided by backtest_v4.py (200 coins, 90 days, both halves, after fees):
# setups that fire, exit style ("structure" = stop beyond the nearest level, targets at the next levels),
# and the filters that improved both halves. ML scorer did NOT beat the rules out-of-sample -> off.
V4 = {"setups": ["RANGE_BREAK", "RETEST", "BOS", "BREAKOUT20"], "exits": "structure",
      "min_room_r": 2.0, "max_atr_pct": 2.5, "ml": False,
      # v4.1 exit plan (backtest_exits.py): 25% at TP1, 25% at TP2, 50% at TP3, stop to entry after TP1.
      # Beat thirds after fees in both halves, with caps and with the coin gate; BTC-trend filter and
      # TP1-farther / later-breakeven variants did not hold in both halves -> off.
      "exit_weights": [0.25, 0.25, 0.5]}
STRATEGY_VERSION = "v4.1"
STRATEGY_NOTE = "chart setups v4 + exits 25/25/50 (stop to entry after TP1) + live news filter"
# Live news filter (news.py; can't be backtested): skip longs on fresh (24h) bearish news for the coin and shorts
# on fresh bullish news (not for BTC/ETH: their headlines are market-wide), show the headline, follow up on open signals.
NEWS = {"veto": True, "followups": True}
