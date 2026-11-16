#!/usr/bin/env python3
"""
build_feed.py — turns output/signals.json into output/bot.json, a compact feed
for the WhatsApp bot (Cloudflare Worker).

Why a separate file: signals.json is ~1 MB of pretty-printed JSON. The Worker
parses the feed on every cold start, so we ship a small array-of-arrays file
(~150 KB) plus pre-computed digest lists.

Daily % change: compared with the latest output/history snapshot before today
(falls back to the previous bot.json).

Only technical fields are used. Fundamentals/sector in signals.json are still
placeholder defaults for most symbols, so the bot does not show them.

Usage:  python bot/build_feed.py            (stdlib only, no pip installs)
"""

import json
import math
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIGNALS = ROOT / "output" / "signals.json"
FEED = ROOT / "output" / "bot.json"
HISTORY = ROOT / "output" / "history"

COLS = ["s", "p", "c", "r", "v", "g", "sl", "a", "w"]
# s=symbol p=price c=%chg vs prev feed (null if unknown) r=RSI14
# v=volume / 20d avg  g=% gap to SMA10  sl=SMA100 slope  a=ATR%  w=52w position %

# Filters so the digest isn't dominated by illiquid penny stocks
MIN_PRICE = 20.0
MIN_VOL = 50_000
TOP_N = 5


def r(x, nd=2):
    # signals.json can contain NaN (not valid JSON for JavaScript) — map to null
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) or math.isinf(x) else round(x, nd)


def load_prev(d):
    """Previous close per symbol: latest output/history snapshot before date d,
    falling back to the previous bot.json."""
    snaps = sorted(p for p in HISTORY.glob("*.json") if p.stem < d) if HISTORY.exists() else []
    if snaps:
        try:
            snap = json.loads(snaps[-1].read_text())
            px = {u["s"]: u["p"] for u in snap["u"] if u.get("p")}
            if px:
                return snap["d"], px
        except Exception:
            pass
    if not FEED.exists():
        return None, {}
    try:
        prev = json.loads(FEED.read_text())
        if not prev.get("d") or prev["d"] > d:
            return None, {}   # stale or out-of-order file — no comparison base
        i_s, i_p = prev["cols"].index("s"), prev["cols"].index("p")
        return prev.get("d"), {row[i_s]: row[i_p] for row in prev["rows"]}
    except Exception:
        return None, {}


def main():
    sig = json.loads(SIGNALS.read_text())
    d = sig["trading_date"]
    prev_d, prev_px = load_prev(d)

    if prev_d == d:
        # Fallback path re-run for the same trading date: keep the earlier comparison base
        old = json.loads(FEED.read_text())
        prev_d = old.get("prev_d")
        i_s, i_p, i_c = (old["cols"].index(k) for k in ("s", "p", "c"))
        prev_px = {}
        for row in old["rows"]:
            if row[i_c] is not None and row[i_p]:
                prev_px[row[i_s]] = row[i_p] / (1 + row[i_c] / 100)

    rows, liquid = [], []
    adv = dec = unch = 0
    for u in sig["universe"]:
        p = u.get("price") or 0
        if p <= 0:
            continue
        pp = prev_px.get(u["symbol"])
        chg = r((p / pp - 1) * 100) if pp else None
        if chg is not None:
            if chg > 0.05:
                adv += 1
            elif chg < -0.05:
                dec += 1
            else:
                unch += 1
        row = [
            u["symbol"], r(p), chg, r(u.get("rsi"), 1), r(u.get("vol_ratio")),
            r(u.get("sma10gap")), r(u.get("sma100slope"), 6), r(u.get("atr_pct")),
            r(u.get("week52pos"), 0),
        ]
        rows.append(row)
        if p >= MIN_PRICE and (u.get("vol") or 0) >= MIN_VOL:
            liquid.append(row)

    with_chg = [x for x in liquid if x[2] is not None]
    digest = {
        "gainers": [x[0] for x in sorted(with_chg, key=lambda x: -x[2])[:TOP_N]],
        "losers": [x[0] for x in sorted(with_chg, key=lambda x: x[2])[:TOP_N]],
        "volume": [x[0] for x in sorted(liquid, key=lambda x: -(x[4] or 0))[:TOP_N]],
        "overbought": sum(1 for x in rows if (x[3] or 0) >= 70),
        "oversold": sum(1 for x in rows if x[3] is not None and x[3] <= 30),
        "above_sma10": sum(1 for x in rows if (x[5] or 0) > 0),
        "breadth": {"adv": adv, "dec": dec, "unch": unch} if with_chg else None,
    }

    feed = {
        "d": d,
        "prev_d": prev_d if prev_px else None,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n": len(rows),
        "cols": COLS,
        "digest": digest,
        "rows": sorted(rows, key=lambda x: x[0]),
    }
    FEED.write_text(json.dumps(feed, separators=(",", ":"), allow_nan=False))
    print(f"bot.json: {len(rows)} stocks for {d} (prev {feed['prev_d']}), "
          f"{FEED.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
