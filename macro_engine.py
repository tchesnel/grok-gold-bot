"""
Macro Engine V4 - Fail-safe
Analyse :
- DXY via Yahoo
- US10Y via FRED
- US2Y via FRED

Important :
Si une source bloque ou répond lentement, le bot continue avec une macro partielle/neutre.
"""

import io
import requests
import pandas as pd
import numpy as np


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Connection": "keep-alive",
}

REQUEST_TIMEOUT = (3, 8)  # 3 sec connexion, 8 sec lecture


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def fetch_yahoo_chart(symbol: str, interval: str = "1h", range_: str = "5d") -> pd.DataFrame:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    params = {
        "interval": interval,
        "range": range_
    }

    r = requests.get(
        url,
        params=params,
        headers=REQUEST_HEADERS,
        timeout=REQUEST_TIMEOUT
    )

    if r.status_code != 200:
        raise RuntimeError(f"Yahoo macro error {r.status_code}: {r.text[:200]}")

    data = r.json()
    result = data.get("chart", {}).get("result")

    if not result:
        raise RuntimeError(f"Aucune donnée Yahoo macro pour {symbol}")

    result = result[0]
    timestamps = result.get("timestamp", [])
    quote = result.get("indicators", {}).get("quote", [{}])[0]

    df = pd.DataFrame({
        "timestamp": timestamps,
        "close": quote.get("close", [])
    })

    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df.dropna(inplace=True)
    df.set_index("timestamp", inplace=True)

    return df


def fetch_fred_csv(series_id: str) -> pd.DataFrame:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"

    r = requests.get(
        url,
        headers=REQUEST_HEADERS,
        timeout=REQUEST_TIMEOUT
    )

    if r.status_code != 200:
        raise RuntimeError(f"FRED error {r.status_code}: {r.text[:200]}")

    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["date", "value"]

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"].replace(".", np.nan), errors="coerce")

    df.dropna(inplace=True)
    df.set_index("date", inplace=True)

    return df.tail(30)


def change_pct(series: pd.Series, periods: int) -> float:
    if len(series) <= periods:
        periods = max(1, len(series) - 1)

    if periods <= 0:
        return 0.0

    old = series.iloc[-periods]
    new = series.iloc[-1]

    if old == 0:
        return 0.0

    return float((new / old - 1) * 100)


def change_bps(series: pd.Series, periods: int = 1) -> float:
    if len(series) <= periods:
        periods = max(1, len(series) - 1)

    if periods <= 0:
        return 0.0

    return float((series.iloc[-1] - series.iloc[-periods]) * 100)


def get_macro_snapshot() -> dict:
    score = 0.0
    details = {}

    # ======================================================
    # DXY
    # ======================================================
    try:
        dxy = fetch_yahoo_chart("DX-Y.NYB", "1h", "5d")
        dxy_change = change_pct(dxy["close"], 24)

        details["dxy_last"] = round(float(dxy["close"].iloc[-1]), 3)
        details["dxy_24h_pct"] = round(float(dxy_change), 3)

        if dxy_change <= -0.25:
            score += 0.35
        elif dxy_change <= -0.10:
            score += 0.20
        elif dxy_change >= 0.25:
            score -= 0.35
        elif dxy_change >= 0.10:
            score -= 0.20

    except Exception as e:
        details["dxy_error"] = str(e)[:120]

    # ======================================================
    # US10Y
    # ======================================================
    try:
        us10y = fetch_fred_csv("DGS10")
        us10y_bps = change_bps(us10y["value"], 1)

        details["us10y_last"] = round(float(us10y["value"].iloc[-1]), 3)
        details["us10y_change_bps"] = round(float(us10y_bps), 2)

        if us10y_bps <= -5:
            score += 0.25
        elif us10y_bps >= 5:
            score -= 0.25

    except Exception as e:
        details["us10y_error"] = str(e)[:120]

    # ======================================================
    # US2Y
    # ======================================================
    try:
        us2y = fetch_fred_csv("DGS2")
        us2y_bps = change_bps(us2y["value"], 1)

        details["us2y_last"] = round(float(us2y["value"].iloc[-1]), 3)
        details["us2y_change_bps"] = round(float(us2y_bps), 2)

        if us2y_bps <= -5:
            score += 0.20
        elif us2y_bps >= 5:
            score -= 0.20

    except Exception as e:
        details["us2y_error"] = str(e)[:120]

    score = clamp(score)

    if score >= 0.45:
        narrative = "Macro favorable à l'or"
    elif score <= -0.45:
        narrative = "Macro défavorable à l'or"
    elif score > 0.10:
        narrative = "Macro légèrement favorable"
    elif score < -0.10:
        narrative = "Macro légèrement défavorable"
    else:
        narrative = "Macro neutre ou données partielles"

    return {
        "score": round(float(score), 3),
        "narrative": narrative,
        "details": details
    }
