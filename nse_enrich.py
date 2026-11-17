#!/usr/bin/env python3
"""
nse_enrich.py — Fetch news (TradingView) and fundamentals (EquityAnalyze/Yahoo)
for the top picks from today's signals, then write:
  output/news.json
  output/fundamentals_enriched.json

Run AFTER nse_pipeline.py so output/signals.json is fresh.
"""

import json, time, math, os, sys
from datetime import datetime, timezone
import requests

SIGNALS_PATH = "output/signals.json"
NEWS_PATH    = "output/news.json"
FUND_PATH    = "output/fundamentals_enriched.json"

TOP_N    = 20   # number of picks to enrich
TIMEOUT  = 10   # seconds per request
DELAY    = 0.4  # polite delay between API calls

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NSE-Cockpit/1.0)",
    "Accept": "application/json",
}

# ── scoring (mirrors the display-only formula in the dashboard) ──────────────
def _score(s):
    r   = float(s.get("rsi", 50) or 50)
    v   = float(s.get("vol_ratio") or 1)
    if math.isnan(v): v = 1
    g   = float(s.get("sma10gap", 0) or 0)
    sl  = float(s.get("sma100slope", 0) or 0)
    w   = float(s.get("week52pos", 0.5) or 0.5)
    c   = float(s.get("day_chg_pct", 0) or 0)
    return (max(0, 100 - abs(r - 65) * 2)
            + min(v * 8, 40)
            + max(0, sl * 500)
            + max(0, g * 2)
            + w * 20
            + max(0, c * 3))


def top_picks(n=TOP_N):
    with open(SIGNALS_PATH) as f:
        sig = json.load(f)
    universe = sig.get("universe", [])
    ranked   = sorted(universe, key=_score, reverse=True)
    return ranked[:n], sig.get("trading_date", datetime.utcnow().strftime("%Y-%m-%d"))


# ── TradingView news ──────────────────────────────────────────────────────────
TV_URL = ("https://news-headlines.tradingview.com/v2/headlines"
          "?client=chart&lang=en&symbol=NSE:{sym}")

def fetch_news(symbol: str) -> list:
    url = TV_URL.format(sym=symbol)
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            items = data if isinstance(data, list) else data.get("items", data.get("data", []))
            out = []
            for item in items[:5]:                      # keep top-5 headlines
                out.append({
                    "id":       item.get("id", ""),
                    "title":    item.get("title", ""),
                    "source":   item.get("source", ""),
                    "published": item.get("published", item.get("time", "")),
                    "url":      item.get("link", item.get("url", "")),
                })
            return out
        else:
            print(f"  [news] {symbol}: HTTP {r.status_code}", flush=True)
    except Exception as e:
        print(f"  [news] {symbol}: {e}", flush=True)
    return []


# ── EquityAnalyze / Yahoo fundamentals ───────────────────────────────────────
EQ_URL = "https://api.equityanalyze.com/api/yahoo/{sym}.NSE"

FUND_FIELDS = [
    "dividenddate", "environrisk", "socialrisk", "governancerisk", "esgscore",
    "market_cap", "revenue", "volume",
    "price_52_week_high", "price_52_week_low",
    "sector", "country",
    "Low1M", "High1M",
    "Perf1W", "Perf1M", "Perf3M", "Perf6M", "PerfY", "PerfYTD",
    "RecommendAll", "average_volume_10d_calc",
]

def fetch_fundamentals(symbol: str) -> dict:
    url = EQ_URL.format(sym=symbol)
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 200:
            raw = r.json()
            # extract only the fields we care about
            return {k: raw.get(k) for k in FUND_FIELDS if k in raw}
        else:
            print(f"  [fund] {symbol}: HTTP {r.status_code}", flush=True)
    except Exception as e:
        print(f"  [fund] {symbol}: {e}", flush=True)
    return {}


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    picks, trading_date = top_picks()
    symbols = [p["symbol"] for p in picks]
    print(f"Enriching {len(symbols)} picks for {trading_date}: {symbols}", flush=True)

    news_map  = {}
    fund_map  = {}
    fetched_at = datetime.now(timezone.utc).isoformat()

    for sym in symbols:
        print(f"  → {sym}", flush=True)

        headlines = fetch_news(sym)
        news_map[sym] = headlines
        time.sleep(DELAY)

        funddata = fetch_fundamentals(sym)
        fund_map[sym] = funddata
        time.sleep(DELAY)

    # ── write output/news.json ──
    news_out = {
        "trading_date": trading_date,
        "fetched_at":   fetched_at,
        "symbols":      symbols,
        "news":         news_map,
    }
    os.makedirs("output", exist_ok=True)
    with open(NEWS_PATH, "w") as f:
        json.dump(news_out, f, separators=(",", ":"))
    print(f"Wrote {NEWS_PATH}  ({os.path.getsize(NEWS_PATH)//1024} KB)", flush=True)

    # ── write output/fundamentals_enriched.json ──
    fund_out = {
        "trading_date": trading_date,
        "fetched_at":   fetched_at,
        "symbols":      symbols,
        "fundamentals": fund_map,
    }
    with open(FUND_PATH, "w") as f:
        json.dump(fund_out, f, separators=(",", ":"))
    print(f"Wrote {FUND_PATH}  ({os.path.getsize(FUND_PATH)//1024} KB)", flush=True)

    print("Enrichment complete.", flush=True)


if __name__ == "__main__":
    main()
