"""
mt5_data.py
Data provider V4

Sources :
- TWELVEDATA : XAU/USD propre via API key
- YAHOO : fallback gratuit mais limité
- MT5 : vrai XAUUSD broker sur Windows/VPS
- BINANCE : fallback PAXG/USDT
"""

import requests
import pandas as pd
from urllib.parse import quote

from config import settings


OHLCV_LIMIT = 300

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Connection": "keep-alive",
}


# ==========================================================
# OUTILS
# ==========================================================

def drop_current_candle(df: pd.DataFrame) -> pd.DataFrame:
    if len(df) > 2:
        return df.iloc[:-1].copy()
    return df.copy()


def clean_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if "volume" not in df.columns:
        df["volume"] = 0

    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)

    df.dropna(subset=["open", "high", "low", "close"], inplace=True)

    return df.tail(OHLCV_LIMIT)


# ==========================================================
# TWELVE DATA
# ==========================================================

def fetch_twelvedata_tf(symbol: str, timeframe: str) -> pd.DataFrame:
    api_key = getattr(settings, "TWELVEDATA_API_KEY", "")

    if not api_key:
        raise RuntimeError(
            "TWELVEDATA_API_KEY manquante dans .env. "
            "Ajoute TWELVEDATA_API_KEY=ta_cle_api"
        )

    interval_map = {
        "15m": "15min",
        "1h": "1h",
        "4h": "4h"
    }

    if timeframe not in interval_map:
        raise ValueError(f"Timeframe Twelve Data non supportée: {timeframe}")

    url = "https://api.twelvedata.com/time_series"

    params = {
        "symbol": symbol,
        "interval": interval_map[timeframe],
        "outputsize": OHLCV_LIMIT,
        "apikey": api_key,
        "format": "JSON"
    }

    r = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=30)

    if r.status_code != 200:
        raise RuntimeError(f"Twelve Data HTTP error {r.status_code}: {r.text}")

    data = r.json()

    if "status" in data and data.get("status") == "error":
        raise RuntimeError(f"Twelve Data error: {data.get('message', data)}")

    values = data.get("values")

    if not values:
        raise RuntimeError(f"Aucune donnée Twelve Data pour {symbol} {timeframe}: {data}")

    df = pd.DataFrame(values)

    if "datetime" not in df.columns:
        raise RuntimeError(f"Format Twelve Data inattendu: {data}")

    df.rename(columns={"datetime": "timestamp"}, inplace=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df.set_index("timestamp", inplace=True)

    # Twelve Data renvoie souvent du plus récent au plus ancien.
    df.sort_index(inplace=True)

    if "volume" not in df.columns:
        df["volume"] = 0

    df = df[["open", "high", "low", "close", "volume"]]
    df = clean_ohlcv(df)
    df = drop_current_candle(df)

    return df.tail(OHLCV_LIMIT)


def fetch_twelvedata_all() -> dict:
    symbol = getattr(settings, "TWELVEDATA_SYMBOL", "XAU/USD")

    return {
        "15m": fetch_twelvedata_tf(symbol, "15m"),
        "1h": fetch_twelvedata_tf(symbol, "1h"),
        "4h": fetch_twelvedata_tf(symbol, "4h")
    }


# ==========================================================
# YAHOO FALLBACK
# ==========================================================

def fetch_yahoo_raw(symbol: str, interval: str, range_: str) -> pd.DataFrame:
    safe_symbol = quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{safe_symbol}"

    params = {
        "interval": interval,
        "range": range_
    }

    r = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=30)

    if r.status_code != 200:
        raise RuntimeError(f"Yahoo error {r.status_code}: {r.text}")

    data = r.json()
    result = data.get("chart", {}).get("result")

    if not result:
        raise RuntimeError(f"Aucune donnée Yahoo pour {symbol}")

    result = result[0]

    timestamps = result.get("timestamp", [])
    quote_data = result.get("indicators", {}).get("quote", [{}])[0]

    if not timestamps:
        raise RuntimeError(f"Aucun timestamp Yahoo pour {symbol}")

    df = pd.DataFrame({
        "timestamp": timestamps,
        "open": quote_data.get("open", []),
        "high": quote_data.get("high", []),
        "low": quote_data.get("low", []),
        "close": quote_data.get("close", []),
        "volume": quote_data.get("volume", [])
    })

    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce")
    df.set_index("timestamp", inplace=True)

    df = clean_ohlcv(df)
    df = drop_current_candle(df)

    return df.tail(OHLCV_LIMIT)


def resample_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    df = df_1h.resample("4h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    })

    df.dropna(subset=["open", "high", "low", "close"], inplace=True)
    df = drop_current_candle(df)

    return df.tail(OHLCV_LIMIT)


def fetch_yahoo_all() -> dict:
    symbol = getattr(settings, "YAHOO_SYMBOL", "GC=F")

    df_15m = fetch_yahoo_raw(symbol, "15m", "30d")
    df_1h = fetch_yahoo_raw(symbol, "1h", "60d")
    df_4h = resample_4h(df_1h)

    return {
        "15m": df_15m,
        "1h": df_1h,
        "4h": df_4h
    }


# ==========================================================
# BINANCE FALLBACK
# ==========================================================

def get_binance_symbol(symbol: str) -> str:
    return symbol.replace("/", "").replace("-", "").upper()


def fetch_binance_tf(symbol: str, timeframe: str) -> pd.DataFrame:
    binance_symbol = get_binance_symbol(symbol)

    url = "https://api.binance.com/api/v3/klines"

    params = {
        "symbol": binance_symbol,
        "interval": timeframe,
        "limit": OHLCV_LIMIT
    }

    r = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=30)

    if r.status_code != 200:
        raise RuntimeError(f"Binance error {r.status_code}: {r.text}")

    raw = r.json()

    df = pd.DataFrame(raw, columns=[
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_asset_volume",
        "number_of_trades",
        "taker_buy_base",
        "taker_buy_quote",
        "ignore"
    ])

    df = df[["timestamp", "open", "high", "low", "close", "volume"]]
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", errors="coerce")
    df.set_index("timestamp", inplace=True)

    df = clean_ohlcv(df)
    df = drop_current_candle(df)

    return df.tail(OHLCV_LIMIT)


def fetch_binance_all() -> dict:
    symbol = getattr(settings, "BINANCE_SYMBOL", "PAXG/USDT")

    return {
        "15m": fetch_binance_tf(symbol, "15m"),
        "1h": fetch_binance_tf(symbol, "1h"),
        "4h": fetch_binance_tf(symbol, "4h")
    }


# ==========================================================
# MT5 DATA
# ==========================================================

def fetch_mt5_tf(symbol: str, timeframe: str) -> pd.DataFrame:
    try:
        import MetaTrader5 as mt5
    except ImportError:
        raise RuntimeError(
            "MetaTrader5 n'est pas installé. "
            "Sur Mac ce package ne fonctionne généralement pas. "
            "Utilise DATA_SOURCE=TWELVEDATA ou DATA_SOURCE=YAHOO."
        )

    tf_map = {
        "15m": mt5.TIMEFRAME_M15,
        "1h": mt5.TIMEFRAME_H1,
        "4h": mt5.TIMEFRAME_H4
    }

    if timeframe not in tf_map:
        raise ValueError(f"Timeframe MT5 non supportée: {timeframe}")

    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"Impossible de sélectionner le symbole MT5: {symbol}")

    rates = mt5.copy_rates_from_pos(symbol, tf_map[timeframe], 1, OHLCV_LIMIT)

    if rates is None or len(rates) == 0:
        raise RuntimeError(f"Aucune donnée MT5 reçue pour {symbol} {timeframe}")

    df = pd.DataFrame(rates)

    df.rename(columns={
        "time": "timestamp",
        "tick_volume": "volume"
    }, inplace=True)

    df = df[["timestamp", "open", "high", "low", "close", "volume"]]
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce")
    df.set_index("timestamp", inplace=True)

    df = clean_ohlcv(df)

    return df.tail(OHLCV_LIMIT)


def fetch_mt5_all() -> dict:
    symbol = getattr(settings, "MT5_SYMBOL", "XAUUSD")

    return {
        "15m": fetch_mt5_tf(symbol, "15m"),
        "1h": fetch_mt5_tf(symbol, "1h"),
        "4h": fetch_mt5_tf(symbol, "4h")
    }


# ==========================================================
# ROUTER PRINCIPAL
# ==========================================================

def fetch_all_timeframes() -> dict:
    source = getattr(settings, "DATA_SOURCE", "TWELVEDATA").upper()

    if source == "TWELVEDATA":
        return fetch_twelvedata_all()

    if source == "YAHOO":
        return fetch_yahoo_all()

    if source == "MT5":
        return fetch_mt5_all()

    if source == "BINANCE":
        return fetch_binance_all()

    raise ValueError(f"DATA_SOURCE invalide: {source}")
