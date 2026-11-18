#!/usr/bin/env python3
"""
bake_cockpit.py
===============
Reads output/signals.json produced by nse_pipeline.py,
scores every stock using the 8-point system, computes allocations,
and writes a fresh NSE_Cockpit.html with today's data baked in.

Run order (after nse_pipeline.py):
    python nse_pipeline.py --exchange NSE
    python bake_cockpit.py

Fields available from signals.json (nse_pipeline.py EODHD edition):
    symbol, name, sector, exchange, price, vol, vol_ratio, cap,
    rsi, atr_pct, sma10gap, sma100slope, week52pos,
    day_chg_pct, pe, pe_threshold, roe, de, revg

8-point scoring (adapted to available fields):
    1. RSI >= 50                → +1
    2. SMA100 slope > 0         → +1  (trend direction)
    3. SMA10 gap > 1%           → +1  (above short-term avg, proxy for ADX/momentum)
    4. Week52 position > 50%    → +1  (in upper half of 52-week range)
    5. Week52 position > 75%    → +1  (near 52-week high, proxy for HH-HL)
    6. RSI >= 60                → +1  (stronger momentum)
    7. Vol ratio > 1.5×         → +1  (above-average volume conviction)
    8. Day change > 0%          → +1  (positive close today)

DO NOT change the allocation formulas (softmax + iterative capping).
"""

import json, sys, math, re
from datetime import date
from pathlib import Path

BASE_DIR     = Path(__file__).parent
SIGNALS_FILE = BASE_DIR / "output" / "signals.json"
TEMPLATE     = BASE_DIR / "NSE_Cockpit.html"
OUTPUT_HTML  = BASE_DIR / "NSE_Cockpit.html"   # overwrite in place


# ── ALLOCATION via softmax + iterative capping (DO NOT CHANGE) ─────────────

def softmax(scores):
    m = max(scores)
    exps = [math.exp(s - m) for s in scores]
    total = sum(exps)
    return [e / total for e in exps]


def compute_allocs(picks, min_alloc=2.0, max_alloc=12.0):
    scores = [p.get("sc", 1) for p in picks]
    allocs = [a * 100 for a in softmax(scores)]
    for _ in range(50):
        floored = [max(a, min_alloc) for a in allocs]
        capped  = [min(a, max_alloc) for a in floored]
        total   = sum(capped)
        if abs(total - 100) < 0.01:
            return capped
        allocs = [a / total * 100 for a in capped]
    return [round(a, 2) for a in allocs]


# ── SCORING (DO NOT CHANGE) ─────────────────────────────────────────────────

def score_stock(s):
    """
    8-point system using fields actually present in signals.json.
    Returns integer 0-8.
    """
    sc = 0
    rsi      = s.get("rsi")      or 0
    sma100   = s.get("sma100slope") or 0
    sma10gap = s.get("sma10gap") or 0
    w52      = s.get("week52pos") or 0
    vol_r    = s.get("vol_ratio") or 0
    chg      = s.get("day_chg_pct") or 0

    if rsi >= 50:      sc += 1   # RSI ≥ 50
    if sma100 > 0:     sc += 1   # long-term trend up
    if sma10gap > 1:   sc += 1   # price above 10-day avg by >1% (momentum proxy)
    if w52 > 50:       sc += 1   # upper half of 52-week range
    if w52 > 75:       sc += 1   # near 52-week high (HH-HL proxy)
    if rsi >= 60:      sc += 1   # stronger RSI momentum
    if vol_r > 1.5:    sc += 1   # strong volume conviction
    if chg > 0:        sc += 1   # positive close today
    return sc


def is_watchout(s):
    rsi   = s.get("rsi")      or 50
    vol_r = s.get("vol_ratio") or 1
    return rsi > 80 or rsi < 25 or vol_r > 5


# ── MAIN ────────────────────────────────────────────────────────────────────

def main():
    if not SIGNALS_FILE.exists():
        sys.exit(f"ERROR: {SIGNALS_FILE} not found — run nse_pipeline.py first.")
    if not TEMPLATE.exists():
        sys.exit(f"ERROR: {TEMPLATE} not found in repo root.")

    print(f"Reading {SIGNALS_FILE} …")
    with open(SIGNALS_FILE) as f:
        signals = json.load(f)

    universe_raw = signals.get("universe", [])
    trading_date = signals.get("trading_date", date.today().isoformat())
    print(f"  {len(universe_raw)} stocks  |  date: {trading_date}")

    # Score every stock
    scored = []
    for s in universe_raw:
        sc = score_stock(s)
        scored.append({
            "s":   s.get("symbol", ""),
            "n":   s.get("name",   s.get("symbol", "")),
            "x":   s.get("sector", "Other"),
            "p":   round(float(s.get("price", 0)),       2),
            "r":   round(float(s.get("rsi",   50)),      1),
            "v":   round(float(s.get("vol_ratio", 1)),   2),
            "g":   round(float(s.get("sma10gap", 0)),    2),
            "c":   round(float(s.get("day_chg_pct", 0)), 2),
            "a":   round(float(s.get("atr_pct", 2)),     2),
            "w":   round(sc * 50, 1),     # legacy weight field
            "sc":  round(sc * 50, 1),     # score used by cockpit JS
            "al":  5.0,                   # placeholder; replaced below for top 100
            "cap": s.get("cap", -1),
        })

    # Sort descending by score
    scored.sort(key=lambda x: x["sc"], reverse=True)

    # Compute real allocations for top 100
    top100  = scored[:100]
    allocs  = compute_allocs(top100)
    for p, al in zip(top100, allocs):
        p["al"] = round(al, 2)
    for p in scored[100:]:
        p["al"] = 1.0

    # Watchouts (RSI extremes or unusual volume among top 200)
    watchouts = [
        {"s": p["s"], "n": p["n"], "x": p["x"],
         "p": p["p"], "r": p["r"], "c": p["c"]}
        for p in scored[:200]
        if is_watchout({"rsi": p["r"], "vol_ratio": p["v"]})
    ][:15]

    green_count = sum(1 for p in scored if p["c"] > 0)
    watch_count = sum(1 for p in scored if 1 <= (p["sc"] / 50) <= 3)

    baked = {
        "trading_date":   trading_date,
        "universe_count": len(scored),
        "picks":          scored,
        "watchouts":      watchouts,
        "green_count":    green_count,
        "watch_count":    watch_count,
    }

    print(f"  Top pick : {scored[0]['s']}  (score {scored[0]['sc']})")
    print(f"  Green    : {green_count}  |  Watchouts: {len(watchouts)}")

    # Read template and inject BAKED
    with open(TEMPLATE) as f:
        html = f.read()

    baked_json = json.dumps(baked, separators=(",", ":"))

    if "{{BAKED_DATA}}" in html:
        html = html.replace("{{BAKED_DATA}}", baked_json)
    else:
        pattern = r'const BAKED\s*=\s*\{.*?\};'
        html, n = re.subn(pattern, f"const BAKED = {baked_json};",
                          html, count=1, flags=re.DOTALL)
        if n == 0:
            sys.exit("ERROR: Could not find BAKED object or {{BAKED_DATA}} placeholder in HTML.")

    with open(OUTPUT_HTML, "w") as f:
        f.write(html)

    print(f"\n✅  Done → {OUTPUT_HTML}")


if __name__ == "__main__":
    main()
