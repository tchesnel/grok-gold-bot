import os
from datetime import datetime
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests


def cfg(name, default=None):
    v = os.getenv(name)
    if v not in (None, ""):
        return v
    try:
        import config
        if hasattr(config, "settings"):
            val = getattr(config.settings, name, None)
            if val not in (None, ""):
                return val
        val = getattr(config, name, None)
        if val not in (None, ""):
            return val
    except Exception:
        pass
    return default


SYMBOL = cfg("SYMBOL", "XAUUSD")
TELEGRAM_BOT_TOKEN = cfg("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = cfg("TELEGRAM_CHAT_ID", "")
MIN_SCORE = float(cfg("V41_MIN_SCORE", "72"))


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] Token ou chat_id manquant.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"[TELEGRAM] Status: {r.status_code}")
        return r.status_code == 200
    except Exception as e:
        print(f"[TELEGRAM] Erreur: {e}")
        return False


def fetch_frames():
    import mt5_data

    fn = getattr(mt5_data, "fetch_all_timeframes", None)
    if not fn:
        raise RuntimeError("fetch_all_timeframes() introuvable dans mt5_data.py")

    for args in [(), (SYMBOL,)]:
        try:
            frames = fn(*args)
            if isinstance(frames, dict):
                return frames
        except TypeError:
            pass

    raise RuntimeError("fetch_all_timeframes() ne retourne pas un dictionnaire.")


def normalize_df(df):
    if df is None or len(df) < 60:
        return None

    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]

    ren = {}
    for final, names in {
        "datetime": ["datetime", "time", "timestamp", "date"],
        "open": ["open", "o"],
        "high": ["high", "h"],
        "low": ["low", "l"],
        "close": ["close", "c"],
        "volume": ["volume", "vol", "v"],
    }.items():
        for n in names:
            if n in df.columns:
                ren[n] = final
                break

    df = df.rename(columns=ren)

    for c in ["open", "high", "low", "close"]:
        if c not in df.columns:
            return None
        df[c] = pd.to_numeric(df[c], errors="coerce")

    if "volume" not in df.columns:
        df["volume"] = 0
    else:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)

    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    return df if len(df) >= 60 else None


def get_tf(frames, tf):
    names = {
        "M15": ["M15", "15m", "15min", "m15", "15"],
        "H1": ["H1", "1h", "60m", "h1", "60"],
        "H4": ["H4", "4h", "240m", "h4", "240"],
    }[tf]

    for k in names:
        if k in frames:
            return normalize_df(frames[k])

    lower = {str(k).lower(): k for k in frames.keys()}
    for k in names:
        if k.lower() in lower:
            return normalize_df(frames[lower[k.lower()]])

    return None


def add_indicators(df):
    df = df.copy()
    close = df["close"]
    high = df["high"]
    low = df["low"]

    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()
    df["ema200"] = close.ewm(span=200, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = (100 - (100 / (1 + rs))).fillna(50)

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    df["atr"] = tr.rolling(14).mean().bfill()
    return df


def wick_info(row):
    rng = max(float(row["high"] - row["low"]), 1e-9)
    body_high = max(float(row["open"]), float(row["close"]))
    body_low = min(float(row["open"]), float(row["close"]))
    upper = (float(row["high"]) - body_high) / rng
    lower = (body_low - float(row["low"])) / rng
    body = abs(float(row["close"] - row["open"])) / rng
    return upper, lower, body


def yahoo_series(symbol, interval="15m", range_="2d"):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}"
        params = {"interval": interval, "range": range_}
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, params=params, headers=headers, timeout=6)
        js = r.json()
        result = js["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        closes = [c for c in closes if c is not None]
        if len(closes) < 20:
            return None
        return pd.Series(closes, dtype=float)
    except Exception:
        return None


def macro_filter():
    dxy = yahoo_series("DX-Y.NYB")
    tnx = yahoo_series("^TNX")

    dxy_bias = "NEUTRAL"
    tnx_bias = "NEUTRAL"

    if dxy is not None:
        dxy_fast = dxy.ewm(span=8, adjust=False).mean().iloc[-1]
        dxy_slow = dxy.ewm(span=21, adjust=False).mean().iloc[-1]
        if dxy.iloc[-1] > dxy_fast > dxy_slow:
            dxy_bias = "UP"
        elif dxy.iloc[-1] < dxy_fast < dxy_slow:
            dxy_bias = "DOWN"

    if tnx is not None:
        tnx_fast = tnx.ewm(span=8, adjust=False).mean().iloc[-1]
        tnx_slow = tnx.ewm(span=21, adjust=False).mean().iloc[-1]
        if tnx.iloc[-1] > tnx_fast > tnx_slow:
            tnx_bias = "UP"
        elif tnx.iloc[-1] < tnx_fast < tnx_slow:
            tnx_bias = "DOWN"

    long_bonus = 0
    short_bonus = 0
    reasons_long = []
    reasons_short = []

    if dxy_bias == "DOWN":
        long_bonus += 10
        reasons_long.append("DXY baissier, favorable or")
    elif dxy_bias == "UP":
        short_bonus += 10
        reasons_short.append("DXY haussier, pression baissière sur l’or")

    if tnx_bias == "DOWN":
        long_bonus += 8
        reasons_long.append("US10Y baissier, favorable or")
    elif tnx_bias == "UP":
        short_bonus += 8
        reasons_short.append("US10Y haussier, pression baissière sur l’or")

    return {
        "dxy_bias": dxy_bias,
        "tnx_bias": tnx_bias,
        "long_bonus": long_bonus,
        "short_bonus": short_bonus,
        "long_reasons": reasons_long,
        "short_reasons": reasons_short,
    }


def analyze(df, label):
    df = add_indicators(df)
    last = df.iloc[-1]
    prev = df.iloc[-2]

    close = float(last["close"])
    atr = float(last["atr"])
    upper, lower, body = wick_info(last)

    prev_12_high = float(df["high"].shift(1).rolling(12).max().iloc[-1])
    prev_12_low = float(df["low"].shift(1).rolling(12).min().iloc[-1])

    prev_30_high = float(df["high"].shift(8).rolling(30).max().iloc[-1])
    prev_30_low = float(df["low"].shift(8).rolling(30).min().iloc[-1])

    recent = df.tail(8)

    bull_sweep = bool(recent["low"].min() < prev_30_low and close > prev_30_low)
    bear_sweep = bool(recent["high"].max() > prev_30_high and close < prev_30_high)

    bos_up = bool(close > prev_12_high)
    bos_down = bool(close < prev_12_low)

    ema_reclaim_up = bool(prev["close"] < prev["ema20"] and close > last["ema20"] and close > last["open"])
    ema_reclaim_down = bool(prev["close"] > prev["ema20"] and close < last["ema20"] and close < last["open"])

    trend_up = bool(close > last["ema50"] and last["ema50"] > last["ema200"])
    trend_down = bool(close < last["ema50"] and last["ema50"] < last["ema200"])

    htf_soft_up = bool(close > last["ema50"] and last["ema20"] > last["ema50"])
    htf_soft_down = bool(close < last["ema50"] and last["ema20"] < last["ema50"])

    top_reject = bool(upper >= 0.45 and close < last["open"])
    bottom_reject = bool(lower >= 0.45 and close > last["open"])

    momentum_5 = (close - float(df["close"].iloc[-6])) / atr if atr > 0 else 0

    flat_market = bool((atr / close) < 0.00055) if close else True
    huge_candle = bool((last["high"] - last["low"]) > 2.0 * atr) if atr > 0 else False

    day_high = float(df["high"].tail(96).max())
    day_low = float(df["low"].tail(96).min())

    near_day_low = bool((close - day_low) <= 0.8 * atr)
    near_day_high = bool((day_high - close) <= 0.8 * atr)

    return {
        "label": label,
        "df": df,
        "close": close,
        "atr": atr,
        "ema20": float(last["ema20"]),
        "ema50": float(last["ema50"]),
        "ema200": float(last["ema200"]),
        "rsi": float(last["rsi"]),
        "trend_up": trend_up,
        "trend_down": trend_down,
        "soft_up": htf_soft_up,
        "soft_down": htf_soft_down,
        "bull_sweep": bull_sweep,
        "bear_sweep": bear_sweep,
        "bos_up": bos_up,
        "bos_down": bos_down,
        "ema_reclaim_up": ema_reclaim_up,
        "ema_reclaim_down": ema_reclaim_down,
        "top_reject": top_reject,
        "bottom_reject": bottom_reject,
        "momentum_5": momentum_5,
        "flat_market": flat_market,
        "huge_candle": huge_candle,
        "prev_12_high": prev_12_high,
        "prev_12_low": prev_12_low,
        "prev_30_high": prev_30_high,
        "prev_30_low": prev_30_low,
        "day_high": day_high,
        "day_low": day_low,
        "near_day_low": near_day_low,
        "near_day_high": near_day_high,
    }


def score_signal(h4, h1, m15, macro):
    price = m15["close"]
    atr = m15["atr"]

    long = 0
    short = 0
    lr = []
    sr = []

    if h4["trend_up"]:
        long += 14
        lr.append("H4 tendance haussière")
    elif h4["soft_up"]:
        long += 8
        lr.append("H4 biais haussier léger")

    if h4["trend_down"]:
        short += 14
        sr.append("H4 tendance baissière")
    elif h4["soft_down"]:
        short += 8
        sr.append("H4 biais baissier léger")

    if h1["trend_up"]:
        long += 16
        lr.append("H1 tendance haussière")
    elif h1["soft_up"]:
        long += 10
        lr.append("H1 biais haussier léger")

    if h1["trend_down"]:
        short += 16
        sr.append("H1 tendance baissière")
    elif h1["soft_down"]:
        short += 10
        sr.append("H1 biais baissier léger")

    if m15["bull_sweep"]:
        long += 24
        lr.append("sweep liquidité bas M15 + réintégration")
    if m15["bear_sweep"]:
        short += 24
        sr.append("sweep liquidité haut M15 + rejet")

    if h1["bull_sweep"]:
        long += 14
        lr.append("sweep liquidité bas H1")
    if h1["bear_sweep"]:
        short += 14
        sr.append("sweep liquidité haut H1")

    if m15["bos_up"]:
        long += 18
        lr.append("cassure structure M15 haussière")
    if m15["bos_down"]:
        short += 18
        sr.append("cassure structure M15 baissière")

    if m15["ema_reclaim_up"]:
        long += 10
        lr.append("reprise EMA20 M15")
    if m15["ema_reclaim_down"]:
        short += 10
        sr.append("perte EMA20 M15")

    if m15["bottom_reject"]:
        long += 10
        lr.append("mèche basse de rejet")
    if m15["top_reject"]:
        short += 10
        sr.append("mèche haute de rejet")

    if m15["near_day_low"] and (m15["bull_sweep"] or m15["bottom_reject"]):
        long += 8
        lr.append("réaction proche low récent")
    if m15["near_day_high"] and (m15["bear_sweep"] or m15["top_reject"]):
        short += 8
        sr.append("réaction proche high récent")

    if m15["momentum_5"] > 0.7:
        long += 7
        lr.append("momentum M15 positif")
    if m15["momentum_5"] < -0.7:
        short += 7
        sr.append("momentum M15 négatif")

    long += macro["long_bonus"]
    short += macro["short_bonus"]
    lr += macro["long_reasons"]
    sr += macro["short_reasons"]

    if m15["flat_market"]:
        long -= 18
        short -= 18

    if m15["huge_candle"]:
        long -= 8
        short -= 8

    continuation_long = (h1["trend_up"] or h4["trend_up"]) and m15["bos_up"] and price > m15["ema20"]
    continuation_short = (h1["trend_down"] or h4["trend_down"]) and m15["bos_down"] and price < m15["ema20"]

    reversal_long = (
        (m15["bull_sweep"] or h1["bull_sweep"])
        and (m15["bos_up"] or m15["ema_reclaim_up"])
        and (m15["bottom_reject"] or m15["momentum_5"] > 0.45)
        and not h4["trend_down"]
    )

    reversal_short = (
        (m15["bear_sweep"] or h1["bear_sweep"])
        and (m15["bos_down"] or m15["ema_reclaim_down"])
        and (m15["top_reject"] or m15["momentum_5"] < -0.45)
        and not h4["trend_up"]
    )

    breakout_long = m15["bos_up"] and h1["soft_up"] and macro["dxy_bias"] != "UP"
    breakout_short = m15["bos_down"] and h1["soft_down"] and macro["dxy_bias"] != "DOWN"

    long_gate = continuation_long or reversal_long or breakout_long
    short_gate = continuation_short or reversal_short or breakout_short

    side = "HOLD"

    if long >= MIN_SCORE and long_gate and long >= short:
        side = "LONG"
        reasons = lr
        score = long
        setup = "Continuation" if continuation_long else "Retournement / opportunité"
        recent_low = float(m15["df"]["low"].tail(12).min())
        sl = recent_low - 0.25 * atr
        risk = price - sl
        tp1 = price + 2.0 * risk
        tp2 = price + 3.0 * risk

    elif short >= MIN_SCORE and short_gate and short > long:
        side = "SHORT"
        reasons = sr
        score = short
        setup = "Continuation" if continuation_short else "Retournement / opportunité"
        recent_high = float(m15["df"]["high"].tail(12).max())
        sl = recent_high + 0.25 * atr
        risk = sl - price
        tp1 = price - 2.0 * risk
        tp2 = price - 3.0 * risk

    else:
        return {
            "side": "HOLD",
            "price": round(price, 2),
            "long_score": round(long, 1),
            "short_score": round(short, 1),
            "long_gate": long_gate,
            "short_gate": short_gate,
            "long_reasons": lr[:8],
            "short_reasons": sr[:8],
            "macro": macro,
            "m15": m15,
            "h1": h1,
            "h4": h4,
        }

    if risk <= 0:
        return {"side": "HOLD", "price": round(price, 2), "long_score": long, "short_score": short, "reason": "risque invalide"}

    risk_pct = risk / price

    if risk_pct < 0.00045:
        return {"side": "HOLD", "price": round(price, 2), "long_score": long, "short_score": short, "reason": "stop trop serré"}
    if risk_pct > 0.010:
        return {"side": "HOLD", "price": round(price, 2), "long_score": long, "short_score": short, "reason": "stop trop large"}

    return {
        "side": side,
        "setup": setup,
        "price": round(price, 2),
        "score": round(score, 1),
        "entry": round(price, 2),
        "sl": round(sl, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2, 2),
        "risk": round(risk, 2),
        "rr1": 2.0,
        "rr2": 3.0,
        "reasons": reasons[:9],
        "long_score": round(long, 1),
        "short_score": round(short, 1),
        "macro": macro,
        "m15": m15,
        "h1": h1,
        "h4": h4,
    }


def format_trade(sig):
    emoji = "🟢" if sig["side"] == "LONG" else "🔴"
    reasons = "\n".join([f"• {r}" for r in sig["reasons"]])

    return f"""<b>{emoji} GROK GOLD V4.1 — {sig['side']} VALIDÉ</b>

<b>Actif :</b> {SYMBOL}
<b>Type :</b> {sig['setup']}
<b>Score qualité :</b> {sig['score']}/100

<b>Entrée zone :</b> {sig['entry']}
<b>Stop loss :</b> {sig['sl']}
<b>TP1 :</b> {sig['tp1']} | RR 1:2
<b>TP2 :</b> {sig['tp2']} | RR 1:3
<b>Risque prix :</b> {sig['risk']}

<b>Macro :</b>
DXY : {sig['macro']['dxy_bias']}
US10Y : {sig['macro']['tnx_bias']}

<b>Raisons :</b>
{reasons}

<i>Signal automatique. Vérifie toujours news USD, spread, taille de position et contexte avant d’entrer.</i>"""


def print_hold(sig):
    m15 = sig.get("m15", {})
    macro = sig.get("macro", {})

    print(
        f"Prix: {sig.get('price')} | Action: HOLD | "
        f"LongScore: {sig.get('long_score')} | ShortScore: {sig.get('short_score')} | "
        f"LongGate: {sig.get('long_gate')} | ShortGate: {sig.get('short_gate')}"
    )

    if "reason" in sig:
        print(f"Reason: {sig['reason']}")

    print(
        "Diagnostic M15 | "
        f"bull_sweep={m15.get('bull_sweep')} | bear_sweep={m15.get('bear_sweep')} | "
        f"bos_up={m15.get('bos_up')} | bos_down={m15.get('bos_down')} | "
        f"ema_up={m15.get('ema_reclaim_up')} | ema_down={m15.get('ema_reclaim_down')} | "
        f"top_reject={m15.get('top_reject')} | bottom_reject={m15.get('bottom_reject')}"
    )

    print(f"Macro | DXY={macro.get('dxy_bias')} | US10Y={macro.get('tnx_bias')}")

    lr = sig.get("long_reasons", [])
    sr = sig.get("short_reasons", [])

    if lr:
        print("Long reasons:", " / ".join(lr))
    if sr:
        print("Short reasons:", " / ".join(sr))


def main():
    print(f"GROK GOLD BOT V4.1 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Symbol: {SYMBOL}")
    print(f"Min score: {MIN_SCORE}")

    frames = fetch_frames()

    m15 = get_tf(frames, "M15")
    h1 = get_tf(frames, "H1")
    h4 = get_tf(frames, "H4")

    if m15 is None or h1 is None or h4 is None:
        raise RuntimeError("Données insuffisantes M15/H1/H4.")

    m15a = analyze(m15, "M15")
    h1a = analyze(h1, "H1")
    h4a = analyze(h4, "H4")
    macro = macro_filter()

    sig = score_signal(h4a, h1a, m15a, macro)

    if sig["side"] == "HOLD":
        print_hold(sig)
        return

    print(
        f"Prix: {sig['price']} | Action: {sig['side']} | Score: {sig['score']} | "
        f"Entry: {sig['entry']} | SL: {sig['sl']} | TP1: {sig['tp1']}"
    )

    send_telegram(format_trade(sig))


if __name__ == "__main__":
    main()
