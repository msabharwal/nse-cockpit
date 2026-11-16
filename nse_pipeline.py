#!/usr/bin/env python3
"""
NSE/BSE Full-Market Data Pipeline — EODHD Edition
===================================================
Fetches OHLCV history via EOD Historical Data API (eodhd.com) for all
listed equities on NSE and BSE (and any other exchange EODHD supports).
Computes RSI-14, ATR-14, SMA-10 gap, SMA-100 slope, vol-ratio, 52-week
range and exports signals.json for the NSE Backtest Lab cockpit.

Setup:
  export EODHD_API_KEY=your_key_here

Usage:
  First run (backfills 1 year of history):
    python nse_pipeline.py --init

  Daily run (run after market close, e.g. 6:30 PM IST Mon–Fri):
    python nse_pipeline.py

  Multiple exchanges (additive — run each separately or together):
    python nse_pipeline.py --exchange NSE
    python nse_pipeline.py --exchange BSE
    python nse_pipeline.py --exchange NSE,BSE

  Scheduled (GitHub Actions cron at 13:30 UTC = 7:00 PM IST):
    See .github/workflows/nse-daily.yml

EODHD endpoints used:
  Exchange symbol list : GET /api/exchange-symbol-list/{EXCHANGE}?api_token=…&fmt=json
  EOD bulk prices      : GET /api/eod-bulk-last-day/{EXCHANGE}?api_token=…&fmt=json&date=YYYY-MM-DD
  Historical OHLCV     : GET /api/eod/{SYMBOL}.{EXCHANGE}?api_token=…&fmt=json&from=…&to=…

Architecture:
  EODHDFetcher   — replaces all bhavcopy fetchers; handles NSE/BSE/NYSE/LSE/etc.
  CacheManager   — unchanged; stores daily CSVs in data/eodhd_cache/
  compute_indicators — unchanged; pure pandas math on OHLCV series
  build_signals_json — unchanged; assembles cockpit JSON
  run_pipeline   — updated to use EODHD; --init backfills 1 year

Requirements:
  pip install requests pandas numpy
"""

import os, sys, json, time, logging, argparse
from datetime import date, timedelta, datetime
from pathlib import Path
import requests
import pandas as pd
import numpy as np

# Optional yfinance — installed lazily in yfinance_backfill()
try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR   = Path(__file__).parent
DATA_DIR   = BASE_DIR / "data"
CACHE_DIR  = DATA_DIR / "eodhd_cache"    # one CSV per trading day per exchange
OUTPUT_DIR = BASE_DIR / "output"

SIGNALS_JSON = OUTPUT_DIR / "signals.json"

# History depth. SMA-100 needs 100 days + buffer. 1 year ≈ 252 trading days.
HISTORY_DAYS = 365

# EODHD base URL
EODHD_BASE = "https://eodhd.com/api"

# Rate limit: EODHD allows ~1000 req/min on paid plans.
# We use bulk endpoint (one request = whole exchange per day) so rate is not a concern.
EODHD_SLEEP = 0.3   # seconds between requests as a courtesy

# Quality filter defaults
MIN_PRICE  = 1.0    # ₹1 / $1 minimum (EODHD returns penny stocks too)
MIN_VOLUME = 1_000  # shares/day minimum

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nse_pipeline")


# ─────────────────────────────────────────────────────────────────────────────
# EODHD CLIENT — thin wrapper around the REST API
# ─────────────────────────────────────────────────────────────────────────────

class EODHDClient:
    """
    Thin wrapper around the EODHD REST API.
    API key is read from the EODHD_API_KEY environment variable.
    Pass api_key= to override (e.g. for testing).
    """

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or os.environ.get("EODHD_API_KEY", "")
        if not self.api_key:
            raise RuntimeError(
                "EODHD API key not found. "
                "Set the EODHD_API_KEY environment variable or pass api_key=."
            )
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "nse-cockpit-pipeline/2.0"})

    def _get(self, path: str, params: dict = None, retries: int = 3) -> list | dict:
        url = f"{EODHD_BASE}/{path}"
        p   = {"api_token": self.api_key, "fmt": "json"}
        if params:
            p.update(params)
        for attempt in range(retries):
            try:
                r = self._session.get(url, params=p, timeout=60)
                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", 10))
                    log.warning(f"Rate limited — sleeping {wait}s")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.exceptions.RequestException as e:
                log.warning(f"Request failed (attempt {attempt+1}/{retries}): {e}")
                time.sleep(2 ** attempt)
        return []

    def exchange_symbols(self, exchange: str) -> list[dict]:
        """
        GET /api/exchange-symbol-list/{EXCHANGE}
        Returns list of {Code, Name, Country, Exchange, Currency, Type, Isin}
        """
        log.info(f"Fetching symbol list for {exchange}…")
        result = self._get(f"exchange-symbol-list/{exchange}")
        return result if isinstance(result, list) else []

    def bulk_eod(self, exchange: str, dt: date) -> list[dict]:
        """
        GET /api/eod-bulk-last-day/{EXCHANGE}?date=YYYY-MM-DD
        Returns one record per symbol with open/high/low/close/volume/adjusted_close.
        Much faster than fetching each symbol individually.
        One API call = entire exchange for one day.
        """
        date_str = dt.strftime("%Y-%m-%d")
        log.info(f"EODHD bulk EOD {exchange} {date_str}…")
        result = self._get(f"eod-bulk-last-day/{exchange}", {"date": date_str})
        return result if isinstance(result, list) else []

    def historical_eod(self, symbol: str, exchange: str,
                       from_date: date, to_date: date) -> list[dict]:
        """
        GET /api/eod/{SYMBOL}.{EXCHANGE}?from=YYYY-MM-DD&to=YYYY-MM-DD
        Returns OHLCV per day for one symbol. Use only during --init for
        symbols missing from bulk cache.
        """
        result = self._get(
            f"eod/{symbol}.{exchange}",
            {"from": from_date.strftime("%Y-%m-%d"),
             "to":   to_date.strftime("%Y-%m-%d"),
             "period": "d"}
        )
        return result if isinstance(result, list) else []

    def fundamentals(self, symbol: str, exchange: str) -> dict:
        """
        GET /api/fundamentals/{SYMBOL}.{EXCHANGE}
        Returns rich fundamental data: sector, industry, market cap, PE, ROE, etc.
        Rate-limited — use sparingly.
        """
        result = self._get(f"fundamentals/{symbol}.{exchange}")
        return result if isinstance(result, dict) else {}


# ─────────────────────────────────────────────────────────────────────────────
# CACHE MANAGER
# Stores one CSV per trading day per exchange: data/eodhd_cache/NSE_YYYYMMDD.csv
# Columns: symbol, open, high, low, close, volume, mktcap_cr
# ─────────────────────────────────────────────────────────────────────────────

class CacheManager:
    def __init__(self, cache_dir: Path = CACHE_DIR):
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)

    def cache_path(self, exchange: str, dt: date) -> Path:
        return self.cache_dir / f"{exchange}_{dt.strftime('%Y%m%d')}.csv"

    def exists(self, exchange: str, dt: date) -> bool:
        return self.cache_path(exchange, dt).exists()

    def save(self, exchange: str, dt: date, df: pd.DataFrame):
        if df.empty:
            return
        path = self.cache_path(exchange, dt)
        df.to_csv(path, index=False)
        log.info(f"Cached {len(df)} rows → {path.name}")

    def load(self, exchange: str, dt: date) -> pd.DataFrame:
        path = self.cache_path(exchange, dt)
        if not path.exists():
            return pd.DataFrame()
        return pd.read_csv(path)

    def available_dates(self, exchange: str) -> list[date]:
        """Return sorted list of cached dates for an exchange."""
        dates = []
        for p in self.cache_dir.glob(f"{exchange}_*.csv"):
            try:
                dt = datetime.strptime(p.stem.split("_")[1], "%Y%m%d").date()
                dates.append(dt)
            except Exception:
                pass
        return sorted(dates)

    def load_history(self, exchange: str, symbol: str,
                     field: str = "close", n: int = 365) -> pd.Series:
        """
        Load time-series of one field for one symbol from cached CSV files.
        Returns a pd.Series indexed by date, oldest first.
        """
        dates = self.available_dates(exchange)[-n:]
        series = {}
        for dt in dates:
            df = self.load(exchange, dt)
            if df.empty or "symbol" not in df.columns:
                continue
            row = df[df["symbol"].str.upper() == symbol.upper()]
            if not row.empty and field in row.columns:
                val = row[field].values[0]
                if pd.notna(val):
                    series[dt] = float(val)
        s = pd.Series(series)
        s.index = pd.DatetimeIndex(s.index)
        return s.sort_index()


# ─────────────────────────────────────────────────────────────────────────────
# EODHD → STANDARD DataFrame CONVERTER
# Converts EODHD bulk-EOD JSON records to the standard OHLCV format used
# by CacheManager and the indicator engine.
# ─────────────────────────────────────────────────────────────────────────────

def bulk_records_to_df(records: list[dict], exchange: str) -> pd.DataFrame:
    """
    Convert EODHD bulk-EOD records to standard DataFrame.
    EODHD bulk record shape:
      {"code":"RELIANCE","exchange_short_name":"NSE","date":"2026-11-15",
       "open":1234.5,"high":1240.0,"low":1230.0,"close":1238.0,
       "adjusted_close":1238.0,"volume":1234567,"market_capitalization":...}
    """
    if not records:
        return pd.DataFrame()

    rows = []
    for r in records:
        try:
            symbol = str(r.get("code", "")).strip().upper()
            if not symbol:
                continue
            # Skip non-equity types if type field present
            typ = str(r.get("type", "Common Stock")).lower()
            if typ not in ("common stock", "equity", ""):
                continue
            close  = float(r.get("adjusted_close") or r.get("close") or 0)
            volume = float(r.get("volume") or 0)
            if close < MIN_PRICE or volume < MIN_VOLUME:
                continue
            rows.append({
                "symbol":     symbol,
                "open":       float(r.get("open")  or close),
                "high":       float(r.get("high")  or close),
                "low":        float(r.get("low")   or close),
                "close":      close,
                "volume":     volume,
                "mktcap_cr":  round(float(r.get("market_capitalization") or 0) / 1e7, 2),
            })
        except (ValueError, TypeError):
            continue

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["symbol"] = df["symbol"].str.strip().str.upper()
    df = df.dropna(subset=["symbol", "close"])
    df = df[df["close"] > 0]
    return df


def historical_records_to_df(symbol: str, records: list[dict]) -> pd.DataFrame:
    """
    Convert EODHD per-symbol historical records to standard DataFrame (one row = one day).
    EODHD historical record: {"date":"2026-11-15","open":...,"high":...,"low":...,"close":...,"volume":...}
    """
    rows = []
    for r in records:
        try:
            close  = float(r.get("adjusted_close") or r.get("close") or 0)
            volume = float(r.get("volume") or 0)
            if close < MIN_PRICE:
                continue
            rows.append({
                "symbol":     symbol.upper(),
                "open":       float(r.get("open")  or close),
                "high":       float(r.get("high")  or close),
                "low":        float(r.get("low")   or close),
                "close":      close,
                "volume":     volume,
                "mktcap_cr":  -1,
            })
        except (ValueError, TypeError):
            continue
    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# INDICATOR ENGINE  (exchange-agnostic — DO NOT CHANGE signal parameters)
# ─────────────────────────────────────────────────────────────────────────────

def calc_rsi(closes: pd.Series, period: int = 14) -> float:
    if len(closes) < period + 1:
        return float("nan")
    delta  = closes.diff().dropna()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_g  = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean().iloc[-1]
    avg_l  = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean().iloc[-1]
    if avg_l == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_g / avg_l), 2)


def calc_atr(highs: pd.Series, lows: pd.Series, closes: pd.Series,
             period: int = 14) -> float:
    if len(closes) < period + 1:
        return float("nan")
    prev_close = closes.shift(1)
    tr = pd.concat([
        highs - lows,
        (highs - prev_close).abs(),
        (lows  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean().iloc[-1]
    last_close = closes.iloc[-1]
    if last_close == 0:
        return float("nan")
    return round(atr / last_close * 100, 4)


def calc_sma_gap(closes: pd.Series, period: int = 10) -> float:
    if len(closes) < period:
        return float("nan")
    sma = closes.tail(period).mean()
    if sma == 0:
        return float("nan")
    return round((closes.iloc[-1] - sma) / sma * 100, 4)


def calc_sma100_slope(closes: pd.Series) -> float:
    if len(closes) < 101:
        return float("nan")
    sma_today = closes.tail(100).mean()
    sma_prev  = closes.iloc[-101:-1].mean()
    if sma_prev == 0:
        return float("nan")
    return round((sma_today - sma_prev) / sma_prev * 100, 6)


def calc_vol_ratio(volumes: pd.Series, avg_period: int = 20) -> float:
    if len(volumes) < avg_period + 1:
        return float("nan")
    avg = volumes.iloc[-avg_period-1:-1].mean()
    if avg == 0:
        return float("nan")
    return round(volumes.iloc[-1] / avg, 4)


def calc_52w(closes: pd.Series) -> float:
    if len(closes) < 2:
        return float("nan")
    window = closes.tail(252)
    hi = window.max()
    lo = window.min()
    if hi == lo:
        return 50.0
    return round((closes.iloc[-1] - lo) / (hi - lo) * 100, 2)


def compute_indicators(symbol: str,
                       highs: pd.Series, lows: pd.Series,
                       closes: pd.Series, volumes: pd.Series) -> dict:
    return {
        "symbol":      symbol,
        "price":       round(float(closes.iloc[-1]), 2),
        "rsi":         calc_rsi(closes),
        "atr_pct":     calc_atr(highs, lows, closes),
        "sma10gap":    calc_sma_gap(closes, 10),
        "sma100slope": calc_sma100_slope(closes),
        "vol":         round(float(volumes.iloc[-1])),
        "vol_ratio":   calc_vol_ratio(volumes),
        "week52pos":   calc_52w(closes),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SECTOR / FUNDAMENTAL DATA
# ─────────────────────────────────────────────────────────────────────────────

FUNDAMENTALS_FILE = DATA_DIR / "fundamentals.json"

# Default sector map for major NSE stocks
DEFAULT_SECTORS = {
    # Banking
    "HDFCBANK":   {"sector": "Banking",      "name": "HDFC Bank"},
    "ICICIBANK":  {"sector": "Banking",      "name": "ICICI Bank"},
    "AXISBANK":   {"sector": "Banking",      "name": "Axis Bank"},
    "KOTAKBANK":  {"sector": "Banking",      "name": "Kotak Mahindra Bank"},
    "SBIN":       {"sector": "Banking",      "name": "State Bank of India"},
    "BANKBARODA": {"sector": "Banking",      "name": "Bank of Baroda"},
    "PNB":        {"sector": "Banking",      "name": "Punjab National Bank"},
    "CANBK":      {"sector": "Banking",      "name": "Canara Bank"},
    "INDUSINDBK": {"sector": "Banking",      "name": "IndusInd Bank"},
    "FEDERALBNK": {"sector": "Banking",      "name": "Federal Bank"},
    "BAJFINANCE": {"sector": "Banking",      "name": "Bajaj Finance"},
    "BAJAJFINSV": {"sector": "Banking",      "name": "Bajaj Finserv"},
    # Technology
    "TCS":        {"sector": "Technology",   "name": "Tata Consultancy Services"},
    "INFY":       {"sector": "Technology",   "name": "Infosys"},
    "WIPRO":      {"sector": "Technology",   "name": "Wipro"},
    "HCLTECH":    {"sector": "Technology",   "name": "HCL Technologies"},
    "TECHM":      {"sector": "Technology",   "name": "Tech Mahindra"},
    "LTIM":       {"sector": "Technology",   "name": "LTIMindtree"},
    "MPHASIS":    {"sector": "Technology",   "name": "Mphasis"},
    "PERSISTENT": {"sector": "Technology",   "name": "Persistent Systems"},
    "COFORGE":    {"sector": "Technology",   "name": "Coforge"},
    "KPITTECH":   {"sector": "Technology",   "name": "KPIT Technologies"},
    # Pharma
    "SUNPHARMA":  {"sector": "Pharma",       "name": "Sun Pharmaceutical"},
    "DRREDDY":    {"sector": "Pharma",       "name": "Dr. Reddy's Laboratories"},
    "CIPLA":      {"sector": "Pharma",       "name": "Cipla"},
    "DIVISLAB":   {"sector": "Pharma",       "name": "Divi's Laboratories"},
    "APOLLOHOSP": {"sector": "Pharma",       "name": "Apollo Hospitals"},
    "AUROPHARMA": {"sector": "Pharma",       "name": "Aurobindo Pharma"},
    "BIOCON":     {"sector": "Pharma",       "name": "Biocon"},
    "IPCALAB":    {"sector": "Pharma",       "name": "IPCA Laboratories"},
    "LAURUSLABS": {"sector": "Pharma",       "name": "Laurus Labs"},
    "ALKEM":      {"sector": "Pharma",       "name": "Alkem Laboratories"},
    # Infra
    "LT":         {"sector": "Infra",        "name": "Larsen & Toubro"},
    "ULTRACEMCO": {"sector": "Infra",        "name": "UltraTech Cement"},
    "AMBUJACEM":  {"sector": "Infra",        "name": "Ambuja Cements"},
    "SHREECEM":   {"sector": "Infra",        "name": "Shree Cement"},
    "GMRINFRA":   {"sector": "Infra",        "name": "GMR Airports"},
    # PSU Power
    "NTPC":       {"sector": "PSU Power",    "name": "NTPC"},
    "POWERGRID":  {"sector": "PSU Power",    "name": "Power Grid Corporation"},
    "NHPC":       {"sector": "PSU Power",    "name": "NHPC"},
    "SJVN":       {"sector": "PSU Power",    "name": "SJVN"},
    "IREDA":      {"sector": "PSU Power",    "name": "IREDA"},
    # Oil & Gas
    "RELIANCE":   {"sector": "Oil & Gas",    "name": "Reliance Industries"},
    "ONGC":       {"sector": "Oil & Gas",    "name": "Oil & Natural Gas Corp"},
    "BPCL":       {"sector": "Oil & Gas",    "name": "Bharat Petroleum"},
    "IOC":        {"sector": "Oil & Gas",    "name": "Indian Oil Corporation"},
    "GAIL":       {"sector": "Oil & Gas",    "name": "GAIL India"},
    "HINDPETRO":  {"sector": "Oil & Gas",    "name": "Hindustan Petroleum"},
    # Auto
    "MARUTI":     {"sector": "Premium Auto", "name": "Maruti Suzuki India"},
    "TATAMOTORS": {"sector": "Premium Auto", "name": "Tata Motors"},
    "M&M":        {"sector": "Premium Auto", "name": "Mahindra & Mahindra"},
    "HEROMOTOCO": {"sector": "Mass Auto",    "name": "Hero MotoCorp"},
    "BAJAJ-AUTO": {"sector": "Mass Auto",    "name": "Bajaj Auto"},
    "EICHERMOT":  {"sector": "Premium Auto", "name": "Eicher Motors"},
    # Consumer / FMCG
    "HINDUNILVR": {"sector": "FMCG",        "name": "Hindustan Unilever"},
    "ITC":        {"sector": "FMCG",        "name": "ITC"},
    "NESTLEIND":  {"sector": "FMCG",        "name": "Nestle India"},
    "BRITANNIA":  {"sector": "FMCG",        "name": "Britannia Industries"},
    "DABUR":      {"sector": "Consumer",    "name": "Dabur India"},
    "MARICO":     {"sector": "Consumer",    "name": "Marico"},
    "GODREJCP":   {"sector": "Consumer",    "name": "Godrej Consumer Products"},
    # Metals
    "TATASTEEL":  {"sector": "Metals",      "name": "Tata Steel"},
    "HINDALCO":   {"sector": "Metals",      "name": "Hindalco Industries"},
    "JSWSTEEL":   {"sector": "Metals",      "name": "JSW Steel"},
    "SAIL":       {"sector": "Metals",      "name": "Steel Authority of India"},
    "VEDL":       {"sector": "Metals",      "name": "Vedanta"},
    # New-age / Consumer tech
    "ZOMATO":     {"sector": "Consumer",    "name": "Zomato"},
    "NYKAA":      {"sector": "Consumer",    "name": "FSN E-Commerce (Nykaa)"},
    "PAYTM":      {"sector": "Technology",  "name": "One 97 Communications (Paytm)"},
    "POLICYBZR":  {"sector": "Banking",     "name": "PB Fintech (Policybazaar)"},
}


def load_fundamentals() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if FUNDAMENTALS_FILE.exists():
        with open(FUNDAMENTALS_FILE) as f:
            return json.load(f)
    # First run — seed from defaults
    save_fundamentals(DEFAULT_SECTORS)
    return dict(DEFAULT_SECTORS)


def save_fundamentals(fund: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(FUNDAMENTALS_FILE, "w") as f:
        json.dump(fund, f, indent=2)


def enrich_fundamentals_from_eodhd(client: EODHDClient, symbols: list[str],
                                    exchange: str, fund: dict) -> dict:
    """
    Pull sector/PE/market-cap from EODHD fundamentals for new symbols.
    Rate-limited — only called for symbols missing from fund dict.
    """
    missing = [s for s in symbols if s not in fund or fund[s].get("sector") == "Technology"]
    if not missing:
        return fund

    log.info(f"Enriching {len(missing)} new symbols from EODHD fundamentals…")
    enriched = 0
    for sym in missing[:200]:   # cap at 200 per run to stay within rate limits
        try:
            data = client.fundamentals(sym, exchange)
            gen  = data.get("General", {})
            hi   = data.get("Highlights", {})
            if not gen:
                continue
            sector = gen.get("Sector") or gen.get("GicSector") or "Technology"
            fund[sym] = {
                "sector": sector,
                "name":   gen.get("Name", sym),
                "pe":     float(hi.get("PERatio") or 20),
                "roe":    float(hi.get("ReturnOnEquityTTM") or 0.12) * 100,
                "de":     float(hi.get("DebtEquityRatio") or 0.5),
                "revg":   float(hi.get("RevenueGrowthQuarterlyYoy") or 0.05) * 100,
                "cap":    round(float(hi.get("MarketCapitalization") or 0) / 1e7, 2),
            }
            enriched += 1
            time.sleep(EODHD_SLEEP)
        except Exception as e:
            log.debug(f"Fundamentals skip {sym}: {e}")

    if enriched:
        log.info(f"Enriched {enriched} symbols from EODHD fundamentals")
        save_fundamentals(fund)
    return fund


# ─────────────────────────────────────────────────────────────────────────────
# SIGNALS BUILDER  (DO NOT CHANGE — cockpit-compatible)
# ─────────────────────────────────────────────────────────────────────────────

PE_THRESHOLD = {
    "Banking": 15, "Technology": 28, "Pharma": 38, "Infra": 22,
    "PSU Power": 18, "Oil & Gas": 14, "Premium Auto": 28, "Mass Auto": 20,
    "Consumer": 65, "FMCG": 60, "Metals": 16,
}
ATR_SECTOR = {
    "Banking": 1.7, "Technology": 1.5, "Pharma": 1.6, "Infra": 1.7,
    "PSU Power": 1.8, "Oil & Gas": 2.0, "Premium Auto": 2.3, "Mass Auto": 2.3,
    "Consumer": 1.6, "FMCG": 1.4, "Metals": 2.2,
}
SECTOR_TREND_FLAG = {
    "Banking": True, "Technology": True, "Pharma": True, "Infra": True,
    "PSU Power": True, "Oil & Gas": False, "Premium Auto": True, "Mass Auto": False,
    "Consumer": True, "FMCG": False, "Metals": False,
}


def build_signals_json(exchanges_active: list[str],
                       indicators: dict[str, dict],
                       fundamentals: dict,
                       trading_date: date) -> dict:
    universe = []
    skipped  = 0

    for symbol, ind in indicators.items():
        fund   = fundamentals.get(symbol, {})
        sector = fund.get("sector", "Technology")

        record = {
            "symbol":      symbol,
            "name":        fund.get("name", symbol),
            "sector":      sector,
            "exchange":    ind.get("exchange", exchanges_active[0]),
            "price":       ind.get("price", 0),
            "vol":         ind.get("vol", 0),
            "vol_ratio":   ind.get("vol_ratio", 1.0),
            "cap":         fund.get("cap", -1),
            "rsi":         ind.get("rsi", 50),
            "atr_pct":     ind.get("atr_pct", ATR_SECTOR.get(sector, 1.5)),
            "sma10gap":    ind.get("sma10gap", 0),
            "sma100slope": ind.get("sma100slope", 0),
            "week52pos":   ind.get("week52pos", 50),
            "pe":          fund.get("pe", 18),
            "pe_threshold":PE_THRESHOLD.get(sector, 25),
            "roe":         fund.get("roe", 12),
            "de":          fund.get("de", 0.5),
            "revg":        fund.get("revg", 5),
        }

        if record["price"] <= 0:
            skipped += 1
            continue

        universe.append(record)

    universe.sort(key=lambda r: r["symbol"])

    output = {
        "generated_at": datetime.now().isoformat(),
        "trading_date": trading_date.isoformat(),
        "stock_count":  len(universe),
        "universe":     universe,
        "meta": {
            "pipeline_version":  "2.0-eodhd",
            "data_source":       "EOD Historical Data (eodhd.com)",
            "indicator_params":  {
                "rsi_period": 14, "atr_period": 14, "sma_short": 10,
                "sma_long": 100, "vol_avg_period": 20, "history_days": HISTORY_DAYS,
            },
            "exchanges_active": exchanges_active,
        },
    }

    log.info(f"Built signals.json: {len(universe)} stocks ({skipped} skipped)")
    return output


# ─────────────────────────────────────────────────────────────────────────────
# HISTORY SNAPSHOT WRITER
# Writes output/history/YYYY-MM-DD.json — compact daily archive used by the
# NSE Backtest Lab to fetch real historical prices for each backtest month.
# ─────────────────────────────────────────────────────────────────────────────

def write_history_snapshot(signals: dict, trading_date: date):
    history_dir  = OUTPUT_DIR / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    history_file = history_dir / f"{trading_date.isoformat()}.json"

    if history_file.exists():
        log.info(f"History snapshot already exists for {trading_date}, skipping.")
        return

    compact = [
        {
            "s":  r["symbol"],
            "p":  r["price"],
            "r":  round(r["rsi"], 2),
            "v":  round(r["vol_ratio"], 4),
            "g":  round(r["sma10gap"], 4),
            "sl": round(r["sma100slope"], 6),
            "a":  round(r["atr_pct"], 4),
            "x":  r["sector"],
            "e":  r.get("exchange", "NSE"),
        }
        for r in signals["universe"]
        if None not in (r["rsi"], r["vol_ratio"], r["sma10gap"])
    ]
    snapshot = {"d": trading_date.isoformat(), "u": compact}
    with open(history_file, "w") as f:
        json.dump(snapshot, f, separators=(',', ':'))
    log.info(f"✓ Wrote history snapshot → {history_file.name}  ({len(compact)} stocks)")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def get_trading_dates(start: date, end: date) -> list[date]:
    """Return weekdays between start and end (Mon–Fri)."""
    dates, d = [], start
    while d <= end:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    return dates


# ─────────────────────────────────────────────────────────────────────────────
# YFINANCE BACKFILL — free historical data for NSE via Yahoo Finance (.NS)
# Used as fallback when EODHD bulk-EOD returns 402 on historical dates.
# ─────────────────────────────────────────────────────────────────────────────

def yfinance_backfill(exchange: str, cache: "CacheManager",
                      start: date, end: date,
                      symbol_list: list[str] = None) -> int:
    """
    Download 1 year of daily OHLCV for all NSE-listed symbols via yfinance
    (.NS suffix) and write them into the EODHD cache CSV format so the rest
    of the pipeline sees them identically.

    Returns the number of trading-day CSVs written.
    """
    global _YF_AVAILABLE
    if not _YF_AVAILABLE:
        log.info("yfinance not installed — installing now…")
        import subprocess
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet",
             "--break-system-packages", "yfinance"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        import yfinance as yf
        _YF_AVAILABLE = True

    import yfinance as yf  # noqa: F811 — may have just been installed

    # Build list of .NS tickers to fetch
    if symbol_list:
        tickers = [f"{s}.NS" for s in symbol_list]
    else:
        # Fallback: use Nifty 500 as the base universe if we have no symbol list
        # (a full NSE download would be 2000+ tickers and take hours)
        log.info("No symbol list provided — assembling full NSE universe from multiple index CSVs")
        try:
            idx = yf.download("^CRSLDX", period="5d", progress=False)  # just to warm up
        except Exception:
            pass
        # Download full NSE universe by combining multiple broad index CSVs.
        # Each CSV covers a different market-cap band; together they reach
        # ~1,800+ actively-traded NSE stocks without hitting the full 2000+
        # symbol yfinance throttle.  Nifty 500 is included as the anchor so
        # we always have a minimal universe even if the other URLs fail.
        nse_index_urls = [
            # Broad / flagship indices (high overlap, deduped below)
            "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
            "https://archives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv",
            # Mid & small cap bands
            "https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
            "https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
            "https://archives.nseindia.com/content/indices/ind_niftymicrocap250_list.csv",
            # Additional broad coverage
            "https://archives.nseindia.com/content/indices/ind_niftylargemidcap250list.csv",
            "https://archives.nseindia.com/content/indices/ind_niftymidsmallcap400list.csv",
        ]
        collected: set[str] = set()
        from io import StringIO
        for url in nse_index_urls:
            try:
                resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                df_idx = pd.read_csv(StringIO(resp.text))
                sym_col = [c for c in df_idx.columns if c.strip().lower() == "symbol"]
                if sym_col:
                    syms = df_idx[sym_col[0]].str.strip().dropna().tolist()
                else:
                    syms = df_idx.iloc[:, 0].str.strip().dropna().tolist()
                before = len(collected)
                collected.update(s for s in syms if s and not s.startswith("#"))
                log.info(f"  {url.split('/')[-1]}: +{len(collected)-before} new symbols "
                         f"(total {len(collected)})")
            except Exception as e:
                log.warning(f"Could not fetch {url.split('/')[-1]}: {e}")
        if collected:
            symbol_list = sorted(collected)
            log.info(f"Full NSE universe assembled: {len(symbol_list)} unique symbols")
        else:
            log.warning("All NSE index URLs failed — using hardcoded 50-stock fallback")
            symbol_list = [
                "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
                "HINDUNILVR", "SBIN", "HDFC", "BHARTIARTL", "KOTAKBANK",
                "ITC", "LT", "AXISBANK", "ASIANPAINT", "MARUTI",
                "SUNPHARMA", "TITAN", "ULTRACEMCO", "BAJFINANCE", "WIPRO",
                "HCLTECH", "POWERGRID", "NTPC", "TECHM", "ONGC",
                "JSWSTEEL", "TATAMOTORS", "INDUSINDBK", "BPCL", "COALINDIA",
                "NESTLEIND", "TATASTEEL", "GRASIM", "CIPLA", "DIVISLAB",
                "DRREDDY", "EICHERMOT", "HEROMOTOCO", "BAJAJ-AUTO", "HINDALCO",
                "APOLLOHOSP", "ADANIPORTS", "SBILIFE", "HDFCLIFE", "BRITANNIA",
                "UPL", "TATACONSUM", "SHREECEM", "BAJAJFINSV", "DABUR",
            ]
        tickers = [f"{s}.NS" for s in symbol_list]

    start_str = start.strftime("%Y-%m-%d")
    end_str   = (end + timedelta(days=1)).strftime("%Y-%m-%d")  # yf end is exclusive

    log.info(f"yfinance backfill: {len(tickers)} NSE symbols, {start_str} → {end_str}")

    # Download all tickers in one batch call (yfinance handles the chunking)
    BATCH = 200   # yfinance handles up to ~1000 but 200 keeps memory low
    all_data: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), BATCH):
        batch = tickers[i:i + BATCH]
        log.info(f"  Downloading batch {i//BATCH + 1} / {-(-len(tickers)//BATCH)} "
                 f"({len(batch)} tickers)…")
        try:
            raw = yf.download(
                batch,
                start=start_str,
                end=end_str,
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            if raw.empty:
                log.warning(f"  Batch {i//BATCH + 1}: no data returned")
                continue

            # yf returns MultiIndex columns (field, ticker) when >1 ticker
            if isinstance(raw.columns, pd.MultiIndex):
                for ticker in batch:
                    try:
                        df_t = raw.xs(ticker, axis=1, level=1).dropna(how="all")
                        if not df_t.empty:
                            all_data[ticker] = df_t
                    except KeyError:
                        pass
            else:
                # Single ticker — raw IS the DataFrame
                if len(batch) == 1:
                    all_data[batch[0]] = raw.dropna(how="all")

        except Exception as e:
            log.warning(f"  Batch download failed: {e}")

    if not all_data:
        log.error("yfinance returned no data at all — backfill aborted")
        return 0

    # Pivot: for each trading date, build one CSV row per symbol
    # Collect all dates across all symbols
    all_dates_set: set[date] = set()
    for df_t in all_data.values():
        for idx_dt in df_t.index:
            d_obj = idx_dt.date() if hasattr(idx_dt, "date") else idx_dt
            all_dates_set.add(d_obj)

    all_dates_sorted = sorted(all_dates_set)
    files_written = 0

    for snap_date in all_dates_sorted:
        if cache.exists(exchange, snap_date):
            continue  # already cached (e.g. from EODHD daily run)

        rows = []
        snap_ts = pd.Timestamp(snap_date)
        for ticker, df_t in all_data.items():
            sym = ticker.replace(".NS", "").upper()
            try:
                row = df_t.loc[snap_ts]
            except KeyError:
                continue
            close  = float(row.get("Close",  row.get("close",  0)) or 0)
            open_  = float(row.get("Open",   row.get("open",   close)) or close)
            high   = float(row.get("High",   row.get("high",   close)) or close)
            low    = float(row.get("Low",    row.get("low",    close)) or close)
            volume = float(row.get("Volume", row.get("volume", 0)) or 0)
            if close <= 0:
                continue
            rows.append({
                "symbol":    sym,
                "open":      round(open_,  2),
                "high":      round(high,   2),
                "low":       round(low,    2),
                "close":     round(close,  2),
                "volume":    int(volume),
                "mktcap_cr": -1,   # not provided by yfinance bulk
            })

        if rows:
            df_day = pd.DataFrame(rows)
            cache.save(exchange, snap_date, df_day)
            files_written += 1

    log.info(f"✓ yfinance backfill complete: {files_written} day-CSVs written "
             f"({len(all_data)} symbols had data)")
    return files_written


def run_pipeline(init_mode: bool = False, exchanges: list[str] = None):
    if exchanges is None:
        exchanges = ["NSE"]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cache  = CacheManager()
    fund   = load_fundamentals()
    client = EODHDClient()      # reads EODHD_API_KEY from env

    today      = date.today()
    all_indicators: dict[str, dict] = {}
    latest_date = today

    for exchange in exchanges:
        log.info(f"═══ Processing {exchange} ═══")

        # ── 1. Download missing bulk-EOD files ───────────────────────────
        if init_mode:
            start = today - timedelta(days=HISTORY_DAYS)
            log.info(f"INIT MODE: backfilling {HISTORY_DAYS} days → {start} to {today}")
        else:
            # Daily mode: just today (and yesterday if today not yet available)
            start = today - timedelta(days=5)   # a few days back to catch holidays

        dates_needed = get_trading_dates(start, today)
        eodhd_ok = False
        for dt in dates_needed:
            if cache.exists(exchange, dt):
                eodhd_ok = True
                continue
            try:
                records = client.bulk_eod(exchange, dt)
                df      = bulk_records_to_df(records, exchange)
                if not df.empty:
                    cache.save(exchange, dt, df)
                    eodhd_ok = True
                time.sleep(EODHD_SLEEP)
            except Exception as e:
                log.warning(f"Bulk EOD failed {exchange} {dt}: {e}")

        # ── 1b. yfinance top-up for NSE full market universe ──────────────────
        # EODHD bulk-EOD only returns the stocks on its plan (typically Nifty 500).
        # We always augment with yfinance for the remaining NSE universe so the
        # dashboard shows the full market (~1800+ stocks), not just Nifty 500.
        # Strategy:
        #   • Fetch the full NSE symbol list from multiple NSE index CSVs.
        #   • Identify symbols NOT already in the EODHD cache for today.
        #   • Fetch those missing symbols via yfinance and merge into cache.
        if exchange == "NSE":
            # --- build full NSE universe from index CSVs ---
            nse_index_urls = [
                "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
                "https://archives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv",
                "https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
                "https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
                "https://archives.nseindia.com/content/indices/ind_niftymicrocap250_list.csv",
                "https://archives.nseindia.com/content/indices/ind_niftylargemidcap250list.csv",
                "https://archives.nseindia.com/content/indices/ind_niftymidsmallcap400list.csv",
            ]
            full_nse_symbols: set[str] = set()
            from io import StringIO as _SIO
            for _url in nse_index_urls:
                try:
                    _resp = requests.get(_url, timeout=30,
                                         headers={"User-Agent": "Mozilla/5.0"})
                    _resp.raise_for_status()
                    _df = pd.read_csv(_SIO(_resp.text))
                    _col = [c for c in _df.columns if c.strip().lower() == "symbol"]
                    _syms = (_df[_col[0]] if _col else _df.iloc[:, 0]).str.strip().dropna()
                    full_nse_symbols.update(s for s in _syms if s and not s.startswith("#"))
                    log.info(f"  {_url.split('/')[-1]}: total unique so far = {len(full_nse_symbols)}")
                except Exception as _e:
                    log.warning(f"  Could not fetch {_url.split('/')[-1]}: {_e}")

            if full_nse_symbols:
                log.info(f"Full NSE index universe: {len(full_nse_symbols)} symbols")
                # Find symbols missing from EODHD cache
                cached_dates = cache.available_dates(exchange)
                cached_syms: set[str] = set()
                if cached_dates:
                    _latest = cache.load(exchange, cached_dates[-1])
                    cached_syms = set(_latest["symbol"].str.upper().tolist())
                missing_syms = sorted(full_nse_symbols - cached_syms)
                log.info(f"Symbols in full universe but not in EODHD cache: {len(missing_syms)}")
                if missing_syms:
                    yfinance_backfill(exchange, cache, start, today,
                                      symbol_list=missing_syms)
            else:
                log.warning("Could not fetch any NSE index CSVs — skipping yfinance top-up")

            # Also fall back fully if init mode produced nothing
            if init_mode:
                cached_days = len(cache.available_dates(exchange))
                if cached_days < 5:
                    log.warning(
                        f"EODHD returned < 5 historical days for {exchange} "
                        f"(got {cached_days}). Running full yfinance backfill…"
                    )
                    yfinance_backfill(exchange, cache, start, today)

        # ── 2. Build indicator time series ───────────────────────────────
        log.info(f"Computing indicators for {exchange}…")

        recent_dates = cache.available_dates(exchange)
        if not recent_dates:
            log.warning(f"No cached data for {exchange}. Skipping.")
            continue

        latest_date  = recent_dates[-1]
        latest_df    = cache.load(exchange, latest_date)
        all_symbols  = latest_df["symbol"].str.upper().unique().tolist()
        log.info(f"{exchange}: {len(all_symbols)} symbols from {latest_date}")

        for i, symbol in enumerate(all_symbols):
            if i % 500 == 0:
                log.info(f"  {i}/{len(all_symbols)} symbols…")

            closes  = cache.load_history(exchange, symbol, "close",  HISTORY_DAYS)
            highs   = cache.load_history(exchange, symbol, "high",   HISTORY_DAYS)
            lows    = cache.load_history(exchange, symbol, "low",    HISTORY_DAYS)
            volumes = cache.load_history(exchange, symbol, "volume", HISTORY_DAYS)

            if len(closes) < 15:
                continue

            idx     = closes.index
            highs   = highs.reindex(idx).fillna(closes)
            lows    = lows.reindex(idx).fillna(closes)
            volumes = volumes.reindex(idx).fillna(0)

            ind = compute_indicators(symbol, highs, lows, closes, volumes)
            ind["exchange"] = exchange
            # Use mktcap from latest day if available
            row = latest_df[latest_df["symbol"].str.upper() == symbol]
            if not row.empty and "mktcap_cr" in row.columns:
                cap = row["mktcap_cr"].values[0]
                if cap > 0:
                    # Merge into fundamentals
                    if symbol not in fund:
                        fund[symbol] = {}
                    fund[symbol]["cap"] = round(float(cap), 2)
            all_indicators[symbol] = ind

        # ── 3. Merge new symbols into fundamentals ───────────────────────
        new_syms = [s for s in all_indicators if s not in fund]
        if new_syms:
            for sym in new_syms:
                fund[sym] = {
                    "sector": "Technology",
                    "name":   sym,
                    "pe":     20.0, "roe": 12.0, "de": 0.5, "revg": 5.0, "cap": -1,
                }
            save_fundamentals(fund)
            log.info(f"Added {len(new_syms)} new symbols to fundamentals.json")

        # ── 4. Optional: enrich fundamentals from EODHD for new symbols ──
        # Only runs if EODHD_ENRICH_FUNDAMENTALS=1 is set (slow — ~1 req/sym)
        if os.environ.get("EODHD_ENRICH_FUNDAMENTALS") == "1":
            fund = enrich_fundamentals_from_eodhd(client, all_symbols, exchange, fund)

    # ── 5. Build and write signals.json ───────────────────────────────────
    signals = build_signals_json(exchanges, all_indicators, fund, latest_date)
    with open(SIGNALS_JSON, "w") as f:
        json.dump(signals, f, indent=2)
    log.info(f"✓ Wrote {SIGNALS_JSON}  ({signals['stock_count']} stocks)")

    # ── 6. Write daily history snapshot(s) ────────────────────────────────
    # In init mode: write one snapshot per historical trading day so the
    # backtest app has real prices going back a full year immediately.
    # In daily mode: just write today's snapshot.
    if init_mode:
        all_dates = cache.available_dates(exchanges[0])  # dates we have data for
        log.info(f"Init mode: writing {len(all_dates)} historical snapshots…")

        # ── Performance fix: load entire cache into memory once ──────────
        # Instead of re-reading CSVs per symbol per date (700k reads), we
        # build one wide DataFrame: rows = (date, symbol), columns = OHLCV.
        # Then slicing by date/symbol is a pure in-memory pandas operation.
        log.info("Loading full cache into memory for fast snapshot generation…")
        all_frames = []
        for dt in all_dates:
            df = cache.load(exchanges[0], dt)
            if df is not None and not df.empty:
                df = df.copy()
                df["_date"] = pd.Timestamp(dt)
                all_frames.append(df)

        if all_frames:
            full_cache = pd.concat(all_frames, ignore_index=True)
            full_cache["symbol"] = full_cache["symbol"].str.upper()
            full_cache = full_cache.set_index(["_date", "symbol"]).sort_index()
            log.info(f"Cache loaded: {len(full_cache)} rows across {len(all_dates)} days")
        else:
            full_cache = None
            log.warning("No cache data found — skipping historical snapshots")

        for snap_date in all_dates:
            history_file = OUTPUT_DIR / "history" / f"{snap_date.isoformat()}.json"
            if history_file.exists():
                continue
            if full_cache is None:
                continue

            snap_ts = pd.Timestamp(snap_date)

            # Symbols traded on this specific day
            if snap_ts not in full_cache.index.get_level_values(0):
                continue
            day_symbols = full_cache.loc[snap_ts].index.tolist()

            # Build per-day indicators using only data up to snap_date
            day_indicators: dict = {}
            # Slice cache: all rows where date <= snap_date
            hist_slice = full_cache.loc[full_cache.index.get_level_values(0) <= snap_ts]

            for symbol in day_symbols:
                try:
                    sym_data = hist_slice.xs(symbol, level="symbol").sort_index()
                except KeyError:
                    continue
                if sym_data.empty or "close" not in sym_data.columns:
                    continue
                closes  = sym_data["close"].dropna()
                highs   = sym_data["high"].reindex(closes.index).fillna(closes)
                lows    = sym_data["low"].reindex(closes.index).fillna(closes)
                volumes = sym_data["volume"].reindex(closes.index).fillna(0)
                if len(closes) < 15:
                    continue
                ind = compute_indicators(symbol, highs, lows, closes, volumes)
                ind["exchange"] = exchanges[0]
                day_indicators[symbol] = ind

            day_signals = build_signals_json(exchanges, day_indicators, fund, snap_date)
            write_history_snapshot(day_signals, snap_date)

        log.info("✓ All historical snapshots written.")
    else:
        write_history_snapshot(signals, latest_date)

    # ── 7. Write pipeline status ──────────────────────────────────────────
    summary = {
        "last_run":        datetime.now().isoformat(),
        "trading_date":    latest_date.isoformat(),
        "stock_count":     signals["stock_count"],
        "exchanges":       exchanges,
        "cache_days":      {ex: len(cache.available_dates(ex)) for ex in exchanges},
        "data_source":     "eodhd.com",
    }
    with open(OUTPUT_DIR / "pipeline_status.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n✓ Pipeline complete: {signals['stock_count']} stocks → {SIGNALS_JSON}")
    print(f"  Exchanges: {', '.join(exchanges)}")
    print(f"  Trading date: {latest_date}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="NSE/BSE Full-Market Data Pipeline via EODHD",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # First run — download 1 year of history for NSE
  EODHD_API_KEY=your_key python nse_pipeline.py --init

  # Daily run — NSE only
  EODHD_API_KEY=your_key python nse_pipeline.py

  # NSE + BSE together
  EODHD_API_KEY=your_key python nse_pipeline.py --exchange NSE,BSE

  # Enrich sector/PE data from EODHD for new symbols
  EODHD_ENRICH_FUNDAMENTALS=1 EODHD_API_KEY=your_key python nse_pipeline.py
""",
    )
    parser.add_argument(
        "--init", action="store_true",
        help=f"Backfill {HISTORY_DAYS} days of history (run once on first setup)"
    )
    parser.add_argument(
        "--exchange", default="NSE",
        help="Comma-separated exchange codes: NSE, BSE, NYSE, LSE, etc. (default: NSE)"
    )
    parser.add_argument(
        "--days", type=int, default=HISTORY_DAYS,
        help=f"Days of history to backfill in --init mode (default: {HISTORY_DAYS})"
    )
    args = parser.parse_args()

    if args.days != HISTORY_DAYS:
        HISTORY_DAYS = args.days

    exchange_list = [e.strip().upper() for e in args.exchange.split(",") if e.strip()]
    run_pipeline(init_mode=args.init, exchanges=exchange_list)
