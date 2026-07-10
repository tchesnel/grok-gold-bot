from pathlib import Path
from datetime import datetime, timezone
import os
import json
import time
import requests
import pandas as pd


CACHE_DIR = Path(".bot_state/hybrid_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

YAHOO_GOLD_SYMBOL = os.getenv("YAHOO_GOLD_SYMBOL", "GC=F")
YAHOO_DXY_SYMBOL = os.getenv("YAHOO_DXY_SYMBOL", "DX-Y.NYB")
YAHOO_US10Y_SYMBOL = os.getenv("YAHOO_US10Y_SYMBOL", "^TNX")

HYBRID_PRIMARY = os.getenv("HYBRID_PRIMARY", "YAHOO").upper()

OANDA_API_TOKEN = os.getenv("OANDA_API_TOKEN", "")
OANDA_ENV = os.getenv("OANDA_ENV", "practice").lower()
OANDA_INSTRUMENT = os.getenv("OANDA_INSTRUMENT", "XAU_USD")

USER_AGENT = "Mozilla/5.0"


def _cache_path(name):
    safe = name.replace("/", "_").replace(":", "_").replace("=", "_").replace("^", "_")
    return CACHE_DIR / f"{safe}.json"


def _read_cache(name, max_age_sec=300):
    p = _cache_path(name)

    if not p.exists():
        return None

    try:
        data = json.loads(p.read_text())
        age = time.time() - float(data.get("saved_at", 0))

        if age <= max_age_sec:
            return data.get("payload")
    except Exception:
        return None

    return None


def _write_cache(name, payload):
    p = _cache_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "saved_at": time.time(),
        "payload": payload,
    }))


def _df_to_payload(df):
    out = df.copy()
    out.index = out.index.astype(str)

    return {
        "index": list(out.index),
        "data": out.to_dict(orient="list"),
    }


def _payload_to_df(payload):
    df = pd.DataFrame(payload["data"])
    df.index = pd.to_datetime(payload["index"], utc=True)
    return df


def yahoo_chart(symbol, interval="5m", range_="5d", cache_sec=55):
    key = f"yahoo_{symbol}_{interval}_{range_}"
    cached = _read_cache(key, max_age_sec=cache_sec)

    if cached:
        return _payload_to_df(cached)

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    params = {
        "interval": interval,
        "range": range_,
        "includePrePost": "true",
    }

    r = requests.get(
        url,
        params=params,
        timeout=10,
        headers={"User-Agent": USER_AGENT},
    )

    if r.status_code != 200:
        raise RuntimeError(f"Yahoo HTTP {r.status_code}: {r.text[:200]}")

    js = r.json()
    result = js.get("chart", {}).get("result")

    if not result:
        raise RuntimeError(f"Yahoo no result for {symbol}: {js}")

    res = result[0]
    ts = res.get("timestamp", [])
    quote = res.get("indicators", {}).get("quote", [{}])[0]

    if not ts or not quote:
        raise RuntimeError(f"Yahoo empty candles for {symbol}")

    df = pd.DataFrame({
        "open": quote.get("open"),
        "high": quote.get("high"),
        "low": quote.get("low"),
        "close": quote.get("close"),
        "volume": quote.get("volume", [0] * len(ts)),
    }, index=pd.to_datetime(ts, unit="s", utc=True))

    df = df.dropna(subset=["open", "high", "low", "close"])

    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close"])

    if len(df) < 20:
        raise RuntimeError(f"Yahoo not enough candles for {symbol} {interval}")

    _write_cache(key, _df_to_payload(df))

    return df


def resample_ohlc(df, rule):
    x = df.copy()

    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }

    out = x.resample(rule).agg(agg).dropna()
    return out


def fetch_yahoo_all():
    # M5 pour réactivité
    m5 = yahoo_chart(YAHOO_GOLD_SYMBOL, "5m", "5d", cache_sec=55)

    # M15 natif si disponible
    try:
        m15 = yahoo_chart(YAHOO_GOLD_SYMBOL, "15m", "10d", cache_sec=120)
    except Exception:
        m15 = resample_ohlc(m5, "15min")

    # H1 natif
    try:
        h1 = yahoo_chart(YAHOO_GOLD_SYMBOL, "60m", "60d", cache_sec=240)
    except Exception:
        h1 = resample_ohlc(m15, "1h")

    # H4 reconstruit depuis H1
    h4 = resample_ohlc(h1, "4h")

    return {
        "M5": m5,
        "5m": m5,
        "M15": m15,
        "15m": m15,
        "H1": h1,
        "1h": h1,
        "H4": h4,
        "4h": h4,
    }


def oanda_base_url():
    if OANDA_ENV == "live":
        return "https://api-fxtrade.oanda.com"
    return "https://api-fxpractice.oanda.com"


def oanda_candles(granularity="M5", count=500, cache_sec=55):
    if not OANDA_API_TOKEN:
        raise RuntimeError("OANDA_API_TOKEN absent")

    key = f"oanda_{OANDA_INSTRUMENT}_{granularity}_{count}"
    cached = _read_cache(key, max_age_sec=cache_sec)

    if cached:
        return _payload_to_df(cached)

    url = f"{oanda_base_url()}/v3/instruments/{OANDA_INSTRUMENT}/candles"

    params = {
        "price": "M",
        "granularity": granularity,
        "count": count,
    }

    headers = {
        "Authorization": f"Bearer {OANDA_API_TOKEN}",
        "User-Agent": USER_AGENT,
    }

    r = requests.get(url, params=params, headers=headers, timeout=10)

    if r.status_code != 200:
        raise RuntimeError(f"OANDA HTTP {r.status_code}: {r.text[:300]}")

    js = r.json()
    candles = js.get("candles", [])

    rows = []

    for c in candles:
        if not c.get("complete", True):
            continue

        mid = c.get("mid", {})
        rows.append({
            "time": c.get("time"),
            "open": float(mid.get("o")),
            "high": float(mid.get("h")),
            "low": float(mid.get("l")),
            "close": float(mid.get("c")),
            "volume": float(c.get("volume", 0)),
        })

    if len(rows) < 20:
        raise RuntimeError(f"OANDA not enough candles {granularity}")

    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df.pop("time"), utc=True)

    _write_cache(key, _df_to_payload(df))

    return df


def fetch_oanda_all():
    m5 = oanda_candles("M5", 500, cache_sec=50)
    m15 = oanda_candles("M15", 500, cache_sec=120)
    h1 = oanda_candles("H1", 500, cache_sec=240)
    h4 = oanda_candles("H4", 500, cache_sec=600)

    return {
        "M5": m5,
        "5m": m5,
        "M15": m15,
        "15m": m15,
        "H1": h1,
        "1h": h1,
        "H4": h4,
        "4h": h4,
    }


def fetch_hybrid_all():
    errors = []

    sources = []

    if HYBRID_PRIMARY == "OANDA":
        sources = ["OANDA", "YAHOO"]
    else:
        sources = ["YAHOO", "OANDA"]

    for src in sources:
        try:
            if src == "OANDA":
                frames = fetch_oanda_all()
            else:
                frames = fetch_yahoo_all()

            print(f"[HYBRID DATA] Source active: {src}")
            return frames

        except Exception as e:
            errors.append(f"{src}: {e}")
            print(f"[HYBRID DATA] {src} indisponible: {e}")

    raise RuntimeError("Hybrid data failed: " + " | ".join(errors))


def fetch_hybrid_m5():
    try:
        if HYBRID_PRIMARY == "OANDA" and OANDA_API_TOKEN:
            df = oanda_candles("M5", 500, cache_sec=50)
            print("[HYBRID M5] OANDA")
            return df
    except Exception as e:
        print(f"[HYBRID M5] OANDA fail: {e}")

    try:
        df = yahoo_chart(YAHOO_GOLD_SYMBOL, "5m", "5d", cache_sec=50)
        print("[HYBRID M5] YAHOO")
        return df
    except Exception as e:
        print(f"[HYBRID M5] Yahoo fail: {e}")
        return None


def simple_bias(df):
    try:
        close = df["close"].astype(float)

        if len(close) < 10:
            return "NEUTRAL"

        last = float(close.iloc[-1])
        prev = float(close.iloc[-8])
        change = last - prev

        if change > 0:
            return "UP"

        if change < 0:
            return "DOWN"

    except Exception:
        pass

    return "NEUTRAL"


def hybrid_macro_filter():
    dxy_bias = "NEUTRAL"
    tnx_bias = "NEUTRAL"

    try:
        dxy = yahoo_chart(YAHOO_DXY_SYMBOL, "15m", "5d", cache_sec=900)
        dxy_bias = simple_bias(dxy)
    except Exception as e:
        print(f"[HYBRID MACRO] DXY fail: {e}")

    try:
        tnx = yahoo_chart(YAHOO_US10Y_SYMBOL, "15m", "5d", cache_sec=900)
        tnx_bias = simple_bias(tnx)
    except Exception as e:
        print(f"[HYBRID MACRO] US10Y fail: {e}")

    print(f"[HYBRID MACRO] DXY={dxy_bias} | US10Y={tnx_bias}")

    return {
        "dxy_bias": dxy_bias,
        "tnx_bias": tnx_bias,
        "source": "HYBRID_FREE",
    }

# === PATCH V6.3.1 — compatibilité score_signal ===
def hybrid_macro_filter():
    dxy_bias = "NEUTRAL"
    tnx_bias = "NEUTRAL"

    try:
        dxy = yahoo_chart(YAHOO_DXY_SYMBOL, "15m", "5d", cache_sec=900)
        dxy_bias = simple_bias(dxy)
    except Exception as e:
        print(f"[HYBRID MACRO] DXY fail: {e}")

    try:
        tnx = yahoo_chart(YAHOO_US10Y_SYMBOL, "15m", "5d", cache_sec=900)
        tnx_bias = simple_bias(tnx)
    except Exception as e:
        print(f"[HYBRID MACRO] US10Y fail: {e}")

    long_bonus = 0
    short_bonus = 0
    long_reasons = []
    short_reasons = []

    # Logique macro pour l'or :
    # DXY baisse = favorable à l'or
    # DXY monte = pression baissière sur l'or
    if dxy_bias == "DOWN":
        long_bonus += 8
        long_reasons.append("DXY baissier, favorable à l’or")
    elif dxy_bias == "UP":
        short_bonus += 8
        short_reasons.append("DXY haussier, pression baissière sur l’or")

    # US10Y baisse = favorable à l'or
    # US10Y monte = pression baissière sur l'or
    if tnx_bias == "DOWN":
        long_bonus += 5
        long_reasons.append("US10Y baissier, favorable à l’or")
    elif tnx_bias == "UP":
        short_bonus += 5
        short_reasons.append("US10Y haussier, pression baissière sur l’or")

    print(
        f"[HYBRID MACRO] DXY={dxy_bias} | US10Y={tnx_bias} | "
        f"LongBonus={long_bonus} | ShortBonus={short_bonus}"
    )

    return {
        "dxy_bias": dxy_bias,
        "tnx_bias": tnx_bias,
        "long_bonus": long_bonus,
        "short_bonus": short_bonus,
        "long_reasons": long_reasons,
        "short_reasons": short_reasons,
        "source": "HYBRID_FREE",
    }
