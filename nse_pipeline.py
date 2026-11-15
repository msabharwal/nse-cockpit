#!/usr/bin/env python3
"""
NSE Full-Market Data Pipeline
==============================
Downloads NSE bhavcopy history → calculates indicators → exports signals.json
for the Daily Trading Cockpit.

Usage:
  First run (downloads ~6 months history):
    python nse_pipeline.py --init

  Daily run (run after 6:30 PM IST each evening):
    python nse_pipeline.py

  Scheduled (cron / Task Scheduler):
    30 18 * * 1-5  python /path/to/nse_pipeline.py   # 6:30 PM IST Mon-Fri

Multi-exchange architecture:
  Each exchange is a 'fetcher' class (NSEFetcher, BSEFetcher, NYSEFetcher, LSEFetcher).
  All fetchers return the same standard OHLCV dict. The indicator engine and
  cockpit JSON writer are exchange-agnostic.

Requirements:
  pip install requests pandas numpy
"""

import os, sys, json, time, zipfile, io, logging, argparse
from datetime import date, timedelta, datetime
from pathlib import Path
import requests
import pandas as pd
import numpy as np

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR   = Path(__file__).parent
DATA_DIR   = BASE_DIR / "data"
CACHE_DIR  = DATA_DIR / "bhavcopy_cache"   # one CSV per trading day
OUTPUT_DIR = BASE_DIR / "output"

# The cockpit reads this file
SIGNALS_JSON = OUTPUT_DIR / "signals.json"

# How many trading days of history to maintain (SMA-100 needs 100, buffer = 120)
HISTORY_DAYS = 180

# Quality filter defaults (can be overridden per exchange)
MIN_MARKET_CAP_CR = 500     # ₹500 Cr minimum (wider than cockpit's ₹2000 Cr — let cockpit filter)
MIN_VOLUME        = 10_000  # shares/day minimum
MIN_PRICE         = 10      # ₹10 minimum

# Request headers to mimic a browser (NSE requires this)
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": "https://www.nseindia.com/",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nse_pipeline")


# ─────────────────────────────────────────────────────────────────────────────
# EXCHANGE FETCHER BASE CLASS
# Multi-exchange architecture: implement one class per exchange.
# Each class must implement:
#   .fetch_bhavcopy(date_obj) → pd.DataFrame with standard columns
#   .fetch_corporate_actions(date_obj) → dict {symbol: "split"/"bonus"/etc}
#   .exchange_code → str (e.g. "NSE", "BSE", "NYSE", "LSE")
# ─────────────────────────────────────────────────────────────────────────────

class ExchangeFetcher:
    exchange_code = "UNKNOWN"

    # Standard column names all fetchers must return
    REQUIRED_COLS = ["symbol", "open", "high", "low", "close", "volume", "mktcap_cr"]

    def fetch_bhavcopy(self, dt: date) -> pd.DataFrame:
        raise NotImplementedError

    def fetch_corporate_actions(self, dt: date) -> dict:
        return {}  # optional — override in subclass

    def _validate(self, df: pd.DataFrame) -> pd.DataFrame:
        for col in self.REQUIRED_COLS:
            if col not in df.columns:
                raise ValueError(f"{self.exchange_code}: missing column '{col}'")
        df["symbol"]    = df["symbol"].str.strip().str.upper()
        df["open"]      = pd.to_numeric(df["open"],   errors="coerce")
        df["high"]      = pd.to_numeric(df["high"],   errors="coerce")
        df["low"]       = pd.to_numeric(df["low"],    errors="coerce")
        df["close"]     = pd.to_numeric(df["close"],  errors="coerce")
        df["volume"]    = pd.to_numeric(df["volume"], errors="coerce")
        df["mktcap_cr"] = pd.to_numeric(df["mktcap_cr"], errors="coerce")
        df = df.dropna(subset=["symbol", "close"])
        df = df[df["close"] > 0]
        return df


# ─────────────────────────────────────────────────────────────────────────────
# NSE FETCHER  (Phase 1 — live today)
# Source: NSE official bhavcopy (free, no API key)
# URL: https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
# ─────────────────────────────────────────────────────────────────────────────

class NSEFetcher(ExchangeFetcher):
    exchange_code = "NSE"

    # NSE bhavcopy URL pattern
    URL_TPL = (
        "https://nsearchives.nseindia.com/content/cm/"
        "BhavCopy_NSE_CM_0_0_0_{date}_F_0000.csv.zip"
    )

    # NSE also publishes mktcap via a separate file; we approximate from close × shares
    # For a full market cap figure, the NSE bulk deal / master file is needed.
    # We use: mktcap_cr ≈ close × (issued_shares / 1e7)   — filled from symbol master

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update(NSE_HEADERS)
        # Warm up session cookie (NSE blocks cold requests)
        try:
            self._session.get("https://www.nseindia.com", timeout=10)
        except Exception:
            pass

    def fetch_bhavcopy(self, dt: date) -> pd.DataFrame:
        date_str = dt.strftime("%Y%m%d")
        url = self.URL_TPL.format(date=date_str)

        log.info(f"NSE bhavcopy {dt} → {url}")
        r = self._session.get(url, timeout=30)
        if r.status_code == 404:
            log.warning(f"NSE: no bhavcopy for {dt} (holiday/weekend?)")
            return pd.DataFrame()
        r.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            csv_name = z.namelist()[0]
            with z.open(csv_name) as f:
                df = pd.read_csv(f)

        # NSE bhavcopy columns (new format, 2024+):
        # TradDt, BizDt, Sgmt, Src, FinInstrmTp, FinInstrmId, ISIN, TckrSymb,
        # SctySrs, XpryDt, FininstrmActlXpryDt, StrkPric, OptnTp, FinInstrmNm,
        # OpnPric, HghPric, LwPric, ClsPric, LastPric, PrvsClsgPric, UndrlygPric,
        # SttlmPric, OpnIntrst, ChngInOpnIntrst, TtlTradgVol, TtlTrfVal, TtlNbOfTxsExctd,
        # SsnId, NewBrdLotQty, Rmks, Rsvd01..04

        # Keep only EQ series (equity, not F&O, ETFs etc.)
        if "SctySrs" in df.columns:
            df = df[df["SctySrs"].str.strip() == "EQ"]

        col_map = {
            "TckrSymb":    "symbol",
            "OpnPric":     "open",
            "HghPric":     "high",
            "LwPric":      "low",
            "ClsPric":     "close",
            "TtlTradgVol": "volume",
        }

        # Fallback for older bhavcopy format (pre-2024):
        if "TckrSymb" not in df.columns:
            col_map = {
                "SYMBOL": "symbol",
                "OPEN":   "open",
                "HIGH":   "high",
                "LOW":    "low",
                "CLOSE":  "close",
                "TOTTRDQTY": "volume",
            }

        df = df.rename(columns=col_map)
        required = ["symbol", "open", "high", "low", "close", "volume"]
        df = df[[c for c in required if c in df.columns]].copy()

        # Market cap: NSE doesn't include this in bhavcopy.
        # We load from the symbol master (fetched separately) or set to -1 for now.
        df["mktcap_cr"] = -1  # will be filled by _enrich_mktcap()

        return self._validate(df)

    def fetch_symbol_master(self) -> pd.DataFrame:
        """
        NSE EQUITY_L.csv — symbol master with market cap and issued shares.
        https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O
        Alternative: https://archives.nseindia.com/content/equities/EQUITY_L.csv
        """
        url = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
        log.info(f"NSE symbol master → {url}")
        try:
            r = self._session.get(url, timeout=30)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            # Columns: SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING,
            #          PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE
            df.columns = [c.strip().upper() for c in df.columns]
            df["symbol"] = df["SYMBOL"].str.strip().str.upper()
            return df
        except Exception as e:
            log.warning(f"Symbol master unavailable: {e}")
            return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# BSE FETCHER STUB  (Phase 2 — ready to implement)
# Source: BSE Bhavcopy — https://www.bseindia.com/download/BhavCopy/Equity/EQ{DDMMYY}_CSV.ZIP
# ─────────────────────────────────────────────────────────────────────────────

class BSEFetcher(ExchangeFetcher):
    exchange_code = "BSE"

    URL_TPL = "https://www.bseindia.com/download/BhavCopy/Equity/EQ{date_bse}_CSV.ZIP"

    def fetch_bhavcopy(self, dt: date) -> pd.DataFrame:
        """
        READY TO IMPLEMENT — uncomment and test.
        BSE bhavcopy columns: CODE, NAME, OPEN, HIGH, LOW, CLOSE, TRDQTY, NO_TRADES, ISIN
        """
        log.info(f"BSE fetcher: stub — skipping {dt}")
        return pd.DataFrame()

        # --- Uncomment to activate BSE ---
        # date_bse = dt.strftime("%d%m%y")
        # url = self.URL_TPL.format(date_bse=date_bse)
        # r = requests.get(url, timeout=30)
        # if r.status_code == 404:
        #     return pd.DataFrame()
        # r.raise_for_status()
        # with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        #     with z.open(z.namelist()[0]) as f:
        #         df = pd.read_csv(f)
        # df = df.rename(columns={"CODE":"symbol","OPEN":"open","HIGH":"high",
        #                         "LOW":"low","CLOSE":"close","TRDQTY":"volume"})
        # df["mktcap_cr"] = -1
        # return self._validate(df)


# ─────────────────────────────────────────────────────────────────────────────
# NYSE FETCHER STUB  (Phase 3)
# Source: Polygon.io free tier / Alpha Vantage / EODHD
# NYSE requires an API key — free tiers available:
#   Polygon.io: 5 req/min free, full history
#   Alpha Vantage: 25 req/day free
#   EODHD: ~$20/month for all exchanges
# ─────────────────────────────────────────────────────────────────────────────

class NYSEFetcher(ExchangeFetcher):
    exchange_code = "NYSE"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    def fetch_bhavcopy(self, dt: date) -> pd.DataFrame:
        """
        READY TO IMPLEMENT — requires API key.
        Set api_key via: export POLYGON_API_KEY=your_key
        Or EODHD: export EODHD_API_KEY=your_key
        """
        log.info(f"NYSE fetcher: stub — set POLYGON_API_KEY to activate")
        return pd.DataFrame()

        # --- Uncomment for Polygon.io ---
        # if not self.api_key:
        #     return pd.DataFrame()
        # date_str = dt.strftime("%Y-%m-%d")
        # url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date_str}"
        # r = requests.get(url, params={"apiKey": self.api_key}, timeout=30)
        # r.raise_for_status()
        # results = r.json().get("results", [])
        # df = pd.DataFrame(results)
        # df = df.rename(columns={"T":"symbol","o":"open","h":"high","l":"low","c":"close","v":"volume"})
        # df["mktcap_cr"] = -1
        # return self._validate(df)


# ─────────────────────────────────────────────────────────────────────────────
# LSE FETCHER STUB  (Phase 3)
# Source: LSE bhavcopy / EODHD / Refinitiv
# ─────────────────────────────────────────────────────────────────────────────

class LSEFetcher(ExchangeFetcher):
    exchange_code = "LSE"

    def fetch_bhavcopy(self, dt: date) -> pd.DataFrame:
        log.info(f"LSE fetcher: stub — set EODHD_API_KEY to activate")
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# INDICATOR ENGINE  (exchange-agnostic)
# Input:  dict {symbol → pd.Series of daily close prices (oldest→newest)}
# Output: dict of computed indicator values per symbol
# DO NOT CHANGE signal parameters — per user constraint
# ─────────────────────────────────────────────────────────────────────────────

def calc_rsi(closes: pd.Series, period: int = 14) -> float:
    """RSI-14. Returns NaN if insufficient data."""
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
    """ATR-14 as % of last close."""
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
    """% gap between last close and SMA-period."""
    if len(closes) < period:
        return float("nan")
    sma = closes.tail(period).mean()
    if sma == 0:
        return float("nan")
    return round((closes.iloc[-1] - sma) / sma * 100, 4)


def calc_sma100_slope(closes: pd.Series) -> float:
    """
    SMA-100 slope = (SMA100_today - SMA100_yesterday) / SMA100_yesterday * 100
    Returns % change. Positive = uptrend.
    """
    if len(closes) < 101:
        return float("nan")
    sma_today = closes.tail(100).mean()
    sma_prev  = closes.iloc[-101:-1].mean()
    if sma_prev == 0:
        return float("nan")
    return round((sma_today - sma_prev) / sma_prev * 100, 6)


def calc_vol_ratio(volumes: pd.Series, avg_period: int = 20) -> float:
    """Today's volume / 20-day average volume."""
    if len(volumes) < avg_period + 1:
        return float("nan")
    avg = volumes.iloc[-avg_period-1:-1].mean()
    if avg == 0:
        return float("nan")
    return round(volumes.iloc[-1] / avg, 4)


def calc_52w(closes: pd.Series) -> float:
    """Position of last close within 52-week range (0–100%)."""
    if len(closes) < 2:
        return float("nan")
    window = closes.tail(252)
    hi = window.max()
    lo = window.min()
    if hi == lo:
        return 50.0
    return round((closes.iloc[-1] - lo) / (hi - lo) * 100, 2)


def compute_indicators(symbol: str,
                       highs: pd.Series,
                       lows:  pd.Series,
                       closes: pd.Series,
                       volumes: pd.Series) -> dict:
    """
    Compute all cockpit indicators for one symbol.
    Returns a dict matching NSE500_UNIVERSE record format.
    """
    return {
        "symbol":      symbol,
        "price":       round(float(closes.iloc[-1]), 2),
        "rsi":         calc_rsi(closes),
        "atr_pct":     calc_atr(highs, lows, closes),
        "sma10gap":    calc_sma_gap(closes, 10),
        "sma100slope": calc_sma100_slope(closes),
        "vol":         round(float(volumes.iloc[-1])),
        "vol_ratio":   calc_vol_ratio(volumes),
    }


# ─────────────────────────────────────────────────────────────────────────────
# CACHE MANAGER
# Stores one CSV per trading day: data/bhavcopy_cache/NSE_YYYYMMDD.csv
# This is the historical store. Each record: symbol, open, high, low, close, volume, mktcap_cr
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
                     field: str = "close", n: int = 180) -> pd.Series:
        """
        Load time-series of one field for one symbol from cached files.
        Returns a pd.Series indexed by date, oldest first.
        """
        dates = self.available_dates(exchange)[-n:]
        series = {}
        for dt in dates:
            df = self.load(exchange, dt)
            if df.empty or "symbol" not in df.columns:
                continue
            row = df[df["symbol"].str.upper() == symbol.upper()]
            if not row.empty:
                val = row[field].values[0]
                series[dt] = float(val) if pd.notna(val) else None
        s = pd.Series(series).dropna()
        s.index = pd.DatetimeIndex(s.index)
        return s


# ─────────────────────────────────────────────────────────────────────────────
# SECTOR / FUNDAMENTAL DATA
# Fundamentals (PE, ROE, D/E, revenue growth) are NOT in bhavcopy.
# They come from: screenscraping screener.in, moneycontrol.com, or a paid API.
# Phase 1: we load from a local fundamentals.json you maintain manually or via
#           a separate quarterly update script.
# Phase 2: auto-fetch from screener.in (free, no API key, requires parsing)
# ─────────────────────────────────────────────────────────────────────────────

FUNDAMENTALS_FILE = DATA_DIR / "fundamentals.json"

# Default sector map for major NSE stocks
# Format: symbol → {sector, pe, roe, de, revg, name}
# This is a starter set — the pipeline appends new symbols with defaults
DEFAULT_SECTORS = {
    # Banking
    "HDFCBANK":  {"sector": "Banking",      "name": "HDFC Bank"},
    "ICICIBANK": {"sector": "Banking",      "name": "ICICI Bank"},
    "AXISBANK":  {"sector": "Banking",      "name": "Axis Bank"},
    "KOTAKBANK": {"sector": "Banking",      "name": "Kotak Mahindra Bank"},
    "SBIN":      {"sector": "Banking",      "name": "State Bank of India"},
    "BANKBARODA":{"sector": "Banking",      "name": "Bank of Baroda"},
    "PNB":       {"sector": "Banking",      "name": "Punjab National Bank"},
    "CANBK":     {"sector": "Banking",      "name": "Canara Bank"},
    "INDUSINDBK":{"sector": "Banking",      "name": "IndusInd Bank"},
    "FEDERALBNK":{"sector": "Banking",      "name": "Federal Bank"},
    # Technology
    "TCS":       {"sector": "Technology",   "name": "Tata Consultancy Services"},
    "INFY":      {"sector": "Technology",   "name": "Infosys"},
    "WIPRO":     {"sector": "Technology",   "name": "Wipro"},
    "HCLTECH":   {"sector": "Technology",   "name": "HCL Technologies"},
    "TECHM":     {"sector": "Technology",   "name": "Tech Mahindra"},
    "LTIM":      {"sector": "Technology",   "name": "LTIMindtree"},
    "MPHASIS":   {"sector": "Technology",   "name": "Mphasis"},
    "PERSISTENT":{"sector": "Technology",   "name": "Persistent Systems"},
    "COFORGE":   {"sector": "Technology",   "name": "Coforge"},
    "KPITTECH":  {"sector": "Technology",   "name": "KPIT Technologies"},
    # Pharma
    "SUNPHARMA": {"sector": "Pharma",       "name": "Sun Pharmaceutical"},
    "DRREDDY":   {"sector": "Pharma",       "name": "Dr. Reddy's Laboratories"},
    "CIPLA":     {"sector": "Pharma",       "name": "Cipla"},
    "DIVISLAB":  {"sector": "Pharma",       "name": "Divi's Laboratories"},
    "APOLLOHOSP":{"sector": "Pharma",       "name": "Apollo Hospitals"},
    "AUROPHARMA":{"sector": "Pharma",       "name": "Aurobindo Pharma"},
    "BIOCON":    {"sector": "Pharma",       "name": "Biocon"},
    "IPCALAB":   {"sector": "Pharma",       "name": "IPCA Laboratories"},
    "LAURUSLABS":{"sector": "Pharma",       "name": "Laurus Labs"},
    "ALKEM":     {"sector": "Pharma",       "name": "Alkem Laboratories"},
    # Infra
    "LT":        {"sector": "Infra",        "name": "Larsen & Toubro"},
    "ULTRACEMCO":{"sector": "Infra",        "name": "UltraTech Cement"},
    "AMBUJACEM": {"sector": "Infra",        "name": "Ambuja Cements"},
    "SHREECEM":  {"sector": "Infra",        "name": "Shree Cement"},
    "GMRINFRA":  {"sector": "Infra",        "name": "GMR Airports"},
    "IRB":       {"sector": "Infra",        "name": "IRB Infrastructure"},
    "KNRCON":    {"sector": "Infra",        "name": "KNR Constructions"},
    "GRINFRA":   {"sector": "Infra",        "name": "G R Infraprojects"},
    # PSU Power
    "NTPC":      {"sector": "PSU Power",    "name": "NTPC"},
    "POWERGRID": {"sector": "PSU Power",    "name": "Power Grid Corporation"},
    "NHPC":      {"sector": "PSU Power",    "name": "NHPC"},
    "SJVN":      {"sector": "PSU Power",    "name": "SJVN"},
    "IREDA":     {"sector": "PSU Power",    "name": "IREDA"},
    "CESC":      {"sector": "PSU Power",    "name": "CESC"},
    # Oil & Gas
    "RELIANCE":  {"sector": "Oil & Gas",    "name": "Reliance Industries"},
    "ONGC":      {"sector": "Oil & Gas",    "name": "Oil & Natural Gas Corp"},
    "BPCL":      {"sector": "Oil & Gas",    "name": "Bharat Petroleum"},
    "IOC":       {"sector": "Oil & Gas",    "name": "Indian Oil Corporation"},
    "GAIL":      {"sector": "Oil & Gas",    "name": "GAIL India"},
    "HINDPETRO": {"sector": "Oil & Gas",    "name": "Hindustan Petroleum"},
    "PETRONET":  {"sector": "Oil & Gas",    "name": "Petronet LNG"},
    # Premium Auto
    "MARUTI":    {"sector": "Premium Auto", "name": "Maruti Suzuki India"},
    "TATAMOTORS":{"sector": "Premium Auto", "name": "Tata Motors"},
    "M&M":       {"sector": "Premium Auto", "name": "Mahindra & Mahindra"},
    "HEROMOTOCO":{"sector": "Mass Auto",    "name": "Hero MotoCorp"},
    "BAJAJ-AUTO":{"sector": "Mass Auto",    "name": "Bajaj Auto"},
    "EICHERMOT": {"sector": "Premium Auto", "name": "Eicher Motors"},
    "BOSCHLTD":  {"sector": "Premium Auto", "name": "Bosch"},
    # Consumer
    "HINDUNILVR":{"sector": "Consumer",     "name": "Hindustan Unilever"},
    "ITC":       {"sector": "Consumer",     "name": "ITC"},
    "NESTLEIND": {"sector": "FMCG",         "name": "Nestle India"},
    "BRITANNIA": {"sector": "FMCG",         "name": "Britannia Industries"},
    "DABUR":     {"sector": "FMCG",         "name": "Dabur India"},
    "MARICO":    {"sector": "FMCG",         "name": "Marico"},
    "GODREJCP":  {"sector": "FMCG",         "name": "Godrej Consumer Products"},
    "TATACONSUM":{"sector": "FMCG",         "name": "Tata Consumer Products"},
    "EMAMILTD":  {"sector": "FMCG",         "name": "Emami"},
    # Metals
    "TATASTEEL": {"sector": "Metals",       "name": "Tata Steel"},
    "JSWSTEEL":  {"sector": "Metals",       "name": "JSW Steel"},
    "HINDALCO":  {"sector": "Metals",       "name": "Hindalco Industries"},
    "VEDL":      {"sector": "Metals",       "name": "Vedanta"},
    "SAIL":      {"sector": "Metals",       "name": "Steel Authority of India"},
    "NMDC":      {"sector": "Metals",       "name": "NMDC"},
    "NATIONALUM":{"sector": "Metals",       "name": "National Aluminium"},
    "HINDCOPPER":{"sector": "Metals",       "name": "Hindustan Copper"},
}

# Default fundamentals — will be overwritten when quarterly update runs
DEFAULT_FUNDAMENTALS = {
    s: {
        "sector": v["sector"],
        "name":   v["name"],
        "pe":     18.0,     # placeholder
        "roe":    15.0,
        "de":     0.5,
        "revg":   8.0,
        "cap":    5000,     # ₹Cr placeholder
    }
    for s, v in DEFAULT_SECTORS.items()
}


def load_fundamentals() -> dict:
    if FUNDAMENTALS_FILE.exists():
        with open(FUNDAMENTALS_FILE) as f:
            return json.load(f)
    # First run — create from defaults
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(FUNDAMENTALS_FILE, "w") as f:
        json.dump(DEFAULT_FUNDAMENTALS, f, indent=2)
    log.info(f"Created starter fundamentals.json ({len(DEFAULT_FUNDAMENTALS)} stocks)")
    return DEFAULT_FUNDAMENTALS


def save_fundamentals(fund: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(FUNDAMENTALS_FILE, "w") as f:
        json.dump(fund, f, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# SIGNALS BUILDER
# Assembles cockpit-ready JSON from indicator results + fundamentals
# ─────────────────────────────────────────────────────────────────────────────

# Sector → PE threshold (DO NOT CHANGE — from cockpit)
PE_THRESHOLD = {
    "Banking": 15, "Technology": 28, "Pharma": 38, "Infra": 22,
    "PSU Power": 18, "Oil & Gas": 14, "Premium Auto": 28, "Mass Auto": 20,
    "Consumer": 65, "FMCG": 60, "Metals": 16,
}

# Sector → ATR multiplier (DO NOT CHANGE — from cockpit)
ATR_SECTOR = {
    "Banking": 1.7, "Technology": 1.5, "Pharma": 1.6, "Infra": 1.7,
    "PSU Power": 1.8, "Oil & Gas": 2.0, "Premium Auto": 2.3, "Mass Auto": 2.3,
    "Consumer": 1.6, "FMCG": 1.4, "Metals": 2.2,
}

# Sector trend flag (DO NOT CHANGE — from cockpit)
SECTOR_TREND_FLAG = {
    "Banking": True, "Technology": True, "Pharma": True, "Infra": True,
    "PSU Power": True, "Oil & Gas": False, "Premium Auto": True, "Mass Auto": False,
    "Consumer": True, "FMCG": False, "Metals": False,
}


def build_signals_json(exchange: str, indicators: dict[str, dict],
                       fundamentals: dict) -> dict:
    """
    Merge indicator results + fundamentals into cockpit-ready JSON.
    Returns the full signals object written to signals.json.
    """
    universe = []
    skipped  = 0

    for symbol, ind in indicators.items():
        fund = fundamentals.get(symbol, {})
        sector = fund.get("sector", "Technology")  # default sector

        record = {
            # Identity
            "symbol":      symbol,
            "name":        fund.get("name", symbol),
            "sector":      sector,
            "exchange":    exchange,

            # Price & volume (from indicator engine)
            "price":       ind.get("price", 0),
            "vol":         ind.get("vol", 0),
            "vol_ratio":   ind.get("vol_ratio", 1.0),
            "cap":         fund.get("cap", -1),  # ₹Cr

            # Technical indicators (DO NOT CHANGE parameters)
            "rsi":         ind.get("rsi", 50),
            "atr_pct":     ind.get("atr_pct", ATR_SECTOR.get(sector, 1.5)),
            "sma10gap":    ind.get("sma10gap", 0),
            "sma100slope": ind.get("sma100slope", 0),

            # Fundamentals
            "pe":          fund.get("pe", 18),
            "pe_threshold":PE_THRESHOLD.get(sector, 25),
            "roe":         fund.get("roe", 12),
            "de":          fund.get("de", 0.5),
            "revg":        fund.get("revg", 5),
        }

        # Basic sanity — skip clearly bad records
        if record["price"] <= 0:
            skipped += 1
            continue

        universe.append(record)

    universe.sort(key=lambda r: r["symbol"])

    output = {
        "generated_at": datetime.now().isoformat(),
        "exchange":     exchange,
        "trading_date": date.today().isoformat(),
        "stock_count":  len(universe),
        "universe":     universe,
        "meta": {
            "pipeline_version": "1.0",
            "indicator_params": {
                "rsi_period": 14, "atr_period": 14, "sma_short": 10,
                "sma_long": 100, "vol_avg_period": 20,
            },
            "exchanges_active":  [exchange],
            "exchanges_ready":   ["NSE", "BSE", "NYSE", "LSE"],
            "notes": "BSE/NYSE/LSE fetchers are stubbed — activate in fetcher classes",
        },
    }

    log.info(f"Built signals.json: {len(universe)} stocks ({skipped} skipped)")
    return output


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def get_trading_dates(start: date, end: date) -> list[date]:
    """Return weekdays between start and end (Mon–Fri)."""
    dates = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # 0=Mon … 4=Fri
            dates.append(d)
        d += timedelta(days=1)
    return dates


def run_pipeline(init_mode: bool = False, exchange: str = "NSE"):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cache  = CacheManager()
    fund   = load_fundamentals()

    # Select fetcher
    fetchers = {
        "NSE":  NSEFetcher(),
        "BSE":  BSEFetcher(),
        "NYSE": NYSEFetcher(api_key=os.environ.get("POLYGON_API_KEY", "")),
        "LSE":  LSEFetcher(),
    }
    fetcher = fetchers[exchange]

    # ── 1. Download missing bhavcopy files ────────────────────────────────
    today = date.today()
    if init_mode:
        start = today - timedelta(days=HISTORY_DAYS)
        log.info(f"INIT MODE: downloading {HISTORY_DAYS} days of history from {start}")
    else:
        start = today  # just today

    dates_needed = get_trading_dates(start, today)
    for dt in dates_needed:
        if cache.exists(exchange, dt):
            log.info(f"Cache hit: {exchange} {dt}")
            continue
        try:
            df = fetcher.fetch_bhavcopy(dt)
            if not df.empty:
                cache.save(exchange, dt, df)
            time.sleep(1.5)  # be polite to NSE servers
        except Exception as e:
            log.warning(f"Failed {exchange} {dt}: {e}")

    # ── 2. Build indicator time series ────────────────────────────────────
    log.info("Computing indicators for all symbols...")

    # Get all symbols from the most recent cached day
    recent_dates = cache.available_dates(exchange)
    if not recent_dates:
        log.error("No cached data found. Run with --init first.")
        sys.exit(1)

    latest_date = recent_dates[-1]
    latest_df   = cache.load(exchange, latest_date)
    all_symbols = latest_df["symbol"].str.upper().unique().tolist()
    log.info(f"Processing {len(all_symbols)} symbols from {latest_date}")

    indicators = {}
    for i, symbol in enumerate(all_symbols):
        if i % 100 == 0:
            log.info(f"  {i}/{len(all_symbols)} symbols...")

        closes  = cache.load_history(exchange, symbol, "close")
        highs   = cache.load_history(exchange, symbol, "high")
        lows    = cache.load_history(exchange, symbol, "low")
        volumes = cache.load_history(exchange, symbol, "volume")

        if len(closes) < 15:  # need at least 15 days for RSI-14
            continue

        # Align all series to same dates
        idx = closes.index
        highs   = highs.reindex(idx).fillna(closes)
        lows    = lows.reindex(idx).fillna(closes)
        volumes = volumes.reindex(idx).fillna(0)

        indicators[symbol] = compute_indicators(
            symbol, highs, lows, closes, volumes
        )

    # ── 3. Merge new symbols into fundamentals ───────────────────────────
    new_syms = 0
    for symbol in indicators:
        if symbol not in fund:
            # New stock — add with defaults
            fund[symbol] = {
                "sector": "Technology",   # default — user can update fundamentals.json
                "name":   symbol,
                "pe":     20.0,
                "roe":    12.0,
                "de":     0.5,
                "revg":   5.0,
                "cap":    -1,
            }
            new_syms += 1
    if new_syms:
        save_fundamentals(fund)
        log.info(f"Added {new_syms} new symbols to fundamentals.json")

    # ── 4. Build and write signals.json ───────────────────────────────────
    signals = build_signals_json(exchange, indicators, fund)
    with open(SIGNALS_JSON, "w") as f:
        json.dump(signals, f, indent=2)
    log.info(f"✓ Wrote {SIGNALS_JSON}  ({signals['stock_count']} stocks)")

    # ── 5. Write a summary ────────────────────────────────────────────────
    summary = {
        "last_run":     datetime.now().isoformat(),
        "trading_date": latest_date.isoformat(),
        "stock_count":  signals["stock_count"],
        "cache_days":   len(recent_dates),
    }
    with open(OUTPUT_DIR / "pipeline_status.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n✓ Pipeline complete: {signals['stock_count']} stocks → {SIGNALS_JSON}")
    print(f"  Copy output/signals.json next to your cockpit.html")
    print(f"  Then set: const USE_LIVE_DATA = true; in cockpit.html")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NSE Full-Market Data Pipeline")
    parser.add_argument(
        "--init", action="store_true",
        help=f"Download last {HISTORY_DAYS} days of history (run once on first setup)"
    )
    parser.add_argument(
        "--exchange", default="NSE", choices=["NSE", "BSE", "NYSE", "LSE"],
        help="Exchange to fetch (default: NSE)"
    )
    parser.add_argument(
        "--days", type=int, default=HISTORY_DAYS,
        help=f"Days of history to download in --init mode (default: {HISTORY_DAYS})"
    )
    args = parser.parse_args()

    if args.days != HISTORY_DAYS:
        HISTORY_DAYS = args.days

    run_pipeline(init_mode=args.init, exchange=args.exchange)
