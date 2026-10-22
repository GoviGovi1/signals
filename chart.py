"""Signal chart PNG in the style of a TradingView long/short position tool (dark theme):
candles of the signal timeframe, entry line, green box up to the final target with TP1/TP2/TP3, red box down to
the stop, price labels on the right axis, pair header with 24h change, 'Spot' / 'Perps 2x' badge, watermark.
usage: chart.render(sig, bars, path) -> path   (bars: [{ts,o,h,l,c}], oldest first)"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyBboxPatch

BG, GRID, TXT, MUTED = "#000000", "#16181d", "#e6e6e6", "#8a8f98"
UP, DN = "#26a69a", "#ef5350"
GREEN_BOX, RED_BOX = "#0f3d1a", "#4a0f14"


def fmt(p):
    if p >= 1000: return f"{p:,.1f}"
    if p >= 100: return f"{p:.2f}"
    if p >= 10: return f"{p:.3f}"
    if p >= 1: return f"{p:.4f}"
    return f"{p:.5g}"


def _label(ax, y, text, color, fg="#ffffff", x=1.005):
    ax.annotate(text, xy=(x, y), xycoords=("axes fraction", "data"), va="center", ha="left", fontsize=10.5,
                color=fg, fontweight="bold", annotation_clip=False,
                bbox=dict(boxstyle="square,pad=0.28", fc=color, ec=color))


def render(s, bars, path, n=80):
    rbox = s.get("range_box")
    if rbox:                                         # show the whole range/base that broke
        L = sum(1 for b in bars if b["ts"] >= rbox[2])
        n = min(max(n, L + 15), 220)
    bars = bars[-n:]
    side = 1 if s["side"] == "LONG" else -1
    entry, sl, tps = s["entry"], s["sl0"], s["tps"]
    last = bars[-1]["c"]
    fig = plt.figure(figsize=(6.7, 10.0), dpi=110, facecolor=BG)
    ax = fig.add_axes([0.03, 0.04, 0.80, 0.86], facecolor=BG)
    N = len(bars)
    future = max(12, N // 4)
    # signal candle position
    k0 = next((k for k, b in enumerate(bars) if b["ts"] >= s.get("bar_ts", bars[-1]["ts"])), N - 1)
    # boxes: from the signal candle to the right edge
    x0, x1 = k0 - 0.5, N + future
    top, bot = (tps[2], entry) if side == 1 else (entry, tps[2])
    ax.add_patch(Rectangle((x0, min(top, bot)), x1 - x0, abs(top - bot), fc=GREEN_BOX, ec="none", alpha=0.85, zorder=1))
    rt, rb = (entry, sl) if side == 1 else (sl, entry)
    ax.add_patch(Rectangle((x0, min(rt, rb)), x1 - x0, abs(rt - rb), fc=RED_BOX, ec="none", alpha=0.9, zorder=1))
    # candles
    w = 0.62 if N <= 100 else 0.7
    for k, b in enumerate(bars):
        col = UP if b["c"] >= b["o"] else DN
        ax.vlines(k, b["l"], b["h"], color=col, lw=1.0, zorder=3)
        lo, hi = sorted((b["o"], b["c"]))
        ax.add_patch(Rectangle((k - w / 2, lo), w, max(hi - lo, (b["h"] - b["l"]) * 0.002 or 1e-12), fc=col, ec=col, zorder=4))
    # chart reading: range/base box and support/resistance levels
    if rbox:
        kr = next((k for k, b in enumerate(bars) if b["ts"] >= rbox[2]), 0)
        ax.add_patch(Rectangle((kr - 0.5, rbox[0]), k0 - kr, rbox[1] - rbox[0], fc="#d29922", alpha=0.08,
                               ec="#d29922", lw=0.9, ls=(0, (3, 3)), zorder=2))
        ax.text(kr, rbox[1], " range", color="#d29922", fontsize=8.5, va="bottom", ha="left", zorder=6)
    ylo = min(min(b["l"] for b in bars), sl, tps[2]); yhi = max(max(b["h"] for b in bars), sl, tps[2])
    for lv in s.get("levels") or []:
        if ylo <= lv <= yhi and min(abs(lv - x) for x in [entry, sl] + list(tps)) > (yhi - ylo) * 0.012:
            ax.hlines(lv, -0.5, x0, color="#d29922", lw=0.8, ls=(0, (6, 4)), alpha=0.75, zorder=2)
            ax.text(0, lv, f"S/R {fmt(lv)}", color="#d29922", fontsize=7.5, va="bottom", ha="left", alpha=0.9, zorder=6)
    # lines
    ax.hlines(entry, x0, x1, color="#9aa0a6", lw=1.1, zorder=5)
    for k, tp in enumerate(tps):
        ax.hlines(tp, x0, x1, color="#3fb950", lw=0.9 if k < 2 else 1.3, ls=(0, (4, 3)) if k < 2 else "-", zorder=5)
        ax.text(x1 - 0.5, tp, f"TP{k + 1}", color="#7ee787", fontsize=9, ha="right",
                va="bottom" if side == 1 else "top", zorder=6, fontweight="bold")
    ax.hlines(sl, x0, x1, color="#f85149", lw=1.2, zorder=5)
    ax.text(x1 - 0.5, sl, "SL", color="#ff7b72", fontsize=9, ha="right", va="top" if side == 1 else "bottom", zorder=6, fontweight="bold")
    ax.text(x0 + 0.5, entry, " ENTRY", color="#c9d1d9", fontsize=9, ha="left", va="bottom" if side == 1 else "top", zorder=6)
    ax.axhline(last, color=UP if last >= bars[-2]["c"] else DN, lw=0.8, ls=(0, (1, 2)), zorder=2)
    # axes
    lo = min(min(b["l"] for b in bars), sl, tps[2]); hi = max(max(b["h"] for b in bars), sl, tps[2])
    pad = (hi - lo) * 0.06
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlim(-1, x1 + 1)
    ax.yaxis.tick_right()
    ax.tick_params(axis="y", colors=MUTED, labelsize=9, length=0)
    ax.tick_params(axis="x", bottom=False, labelbottom=False)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: fmt(v)))
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.grid(axis="y", color=GRID, lw=0.6)
    # right-axis labels, nudged apart so they never overlap
    good = (last - entry) * side >= 0
    labs = [(tps[2], "#2ea043"), (entry, "#6e7681"), (sl, "#da3633"), (last, UP if good else DN)]
    gap = (hi - lo + 2 * pad) * 0.032
    labs.sort(key=lambda l: l[0])
    ys = [l[0] for l in labs]
    for k in range(1, len(ys)):
        ys[k] = max(ys[k], ys[k - 1] + gap)
    for (y0, col), y in zip(labs, ys):
        _label(ax, y, fmt(y0), col)
    # header
    pair = s["coin"] + (" / USDT" if s["market"] != "DEX" else f" ({s.get('chain', 'DEX')})")
    ref = bars[-25]["c"] if len(bars) > 25 else bars[0]["c"]
    ch = last - ref
    fig.text(0.04, 0.955, pair, color=TXT, fontsize=17, fontweight="bold", va="center")
    fig.text(0.04, 0.925, f"{fmt(last)}  {ch:+.4g} ({ch / ref * 100:+.2f}%) 24h", color=UP if ch >= 0 else DN, fontsize=11.5, va="center")
    badge = s.get("trade_on") or ("Spot" if s["market"] == "SPOT" else f"Perps {s.get('lev', 1)}x" if s["market"] == "PERP" else "On-chain spot")
    side_txt, side_col = ("LONG", "#238636") if side == 1 else ("SHORT", "#da3633")
    fig.text(0.97, 0.955, f" {side_txt} ", color="#ffffff", fontsize=11, fontweight="bold", ha="right", va="center",
             bbox=dict(boxstyle="round,pad=0.35", fc=side_col, ec=side_col))
    fig.text(0.97, 0.922, f" {badge} ", color="#ffffff", fontsize=10.5, fontweight="bold", ha="right", va="center",
             bbox=dict(boxstyle="round,pad=0.3", fc="#1f6feb" if "Perps" in badge else "#8957e5", ec="none"))
    rr = abs(tps[2] - entry) / abs(entry - sl) if entry != sl else 0
    fig.text(0.04, 0.015, f"1h chart · Target {abs(tps[2] / entry - 1) * 100:.1f}% · Stop {abs(sl / entry - 1) * 100:.1f}% · R:R {rr:.1f}",
             color=MUTED, fontsize=9, va="bottom")
    fig.text(0.42, 0.5, "Mho Signals", color="#ffffff", alpha=0.06, fontsize=34, fontweight="bold", ha="center", va="center", rotation=0)
    fig.text(0.97, 0.015, "Mho Signals · not financial advice", color=MUTED, fontsize=8, ha="right", va="bottom")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fig.savefig(path, facecolor=BG, dpi=100)
    plt.close(fig)
    return path
