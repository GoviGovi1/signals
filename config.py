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
