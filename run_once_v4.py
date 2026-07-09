import os
import math
import json
import time
from datetime import datetime, timezone

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
        raise RuntimeError("Impossible de trouver fetch_all_timeframes() dans mt5_data.py")

    for args in [(), (SYMBOL,)]:
        try:
            frames = fn(*args)
            if isinstance(frames, dict):
                return frames
        except TypeError:
            continue

    raise RuntimeError("fetch_all_timeframes() ne retourne pas un dictionnaire de timeframes.")


def normalize_df(df):
    if df is None or len(df) < 80:
        return None

    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]

    aliases = {
        "datetime": ["datetime", "time", "timestamp", "date"],
        "open": ["open", "o"],
        "high": ["high", "h"],
        "low": ["low", "l"],
        "close": ["close", "c"],
        "volume": ["volume", "vol", "v"],
    }

    renamed = {}
    for final, names in aliases.items():
        for n in names:
            if n in df.columns:
                renamed[n] = final
                break

    df = df.rename(columns=renamed)

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
    return df if len(df) >= 80 else None


def get_tf(frames, name):
    candidates = {
        "M15": ["M15", "15m", "15min", "m15", "15"],
        "H1": ["H1", "1h", "60m", "h1", "60"],
        "H4": ["H4", "4h", "240m", "h4", "240"],
    }[name]

    for k in candidates:
        if k in frames:
            return normalize_df(frames[k])

    lower_map = {str(k).lower(): k for k in frames.keys()}
    for k in candidates:
        lk = str(k).lower()
        if lk in lower_map:
            return normalize_df(frames[lower_map[lk]])

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
    df["rsi"] = 100 - (100 / (1 + rs))
    df["rsi"] = df["rsi"].fillna(50)

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

    w = 3
    df["swing_high"] = df["high"].eq(df["high"].rolling(2 * w + 1, center=True).max())
    df["swing_low"] = df["low"].eq(df["low"].rolling(2 * w + 1, center=True).min())

    return df


def candle_wicks(row):
    rng = max(row["high"] - row["low"], 1e-9)
    body_high = max(row["open"], row["close"])
    body_low = min(row["open"], row["close"])
    upper = (row["high"] - body_high) / rng
    lower = (body_low - row["low"]) / rng
    body = abs(row["close"] - row["open"]) / rng
    return upper, lower, body


def last_swing_levels(df):
    safe = df.iloc[:-2].copy()
    highs = safe[safe["swing_high"]]
    lows = safe[safe["swing_low"]]

    last_high = float(highs["high"].iloc[-1]) if len(highs) else float(df["high"].tail(30).max())
    last_low = float(lows["low"].iloc[-1]) if len(lows) else float(df["low"].tail(30).min())

    return last_high, last_low, highs, lows


def detect_divergence(df):
    safe = df.iloc[:-1].copy()
    highs = safe[safe["swing_high"]].tail(3)
    lows = safe[safe["swing_low"]].tail(3)

    bull = False
    bear = False

    if len(lows) >= 2:
        a, b = lows.iloc[-2], lows.iloc[-1]
        bull = b["low"] < a["low"] and b["rsi"] > a["rsi"]

    if len(highs) >= 2:
        a, b = highs.iloc[-2], highs.iloc[-1]
        bear = b["high"] > a["high"] and b["rsi"] < a["rsi"]

    return bull, bear


def analyze_tf(df, label):
    df = add_indicators(df)
    last = df.iloc[-1]
    prev = df.iloc[-2]
    last_high, last_low, highs, lows = last_swing_levels(df)
    bull_div, bear_div = detect_divergence(df)

    recent = df.tail(4)
    atr = float(last["atr"])
    close = float(last["close"])
    atr_pct = atr / close if close else 0

    upper_wick, lower_wick, body_ratio = candle_wicks(last)

    trend_up = close > last["ema50"] and last["ema50"] > last["ema200"]
    trend_down = close < last["ema50"] and last["ema50"] < last["ema200"]

    bos_up = prev["close"] <= last_high and close > last_high
    bos_down = prev["close"] >= last_low and close < last_low

    bull_sweep = recent["low"].min() < last_low and close > last_low
    bear_sweep = recent["high"].max() > last_high and close < last_high

    bottom_reject = lower_wick >= 0.48 and close > last["open"]
    top_reject = upper_wick >= 0.48 and close < last["open"]

    huge_candle = (last["high"] - last["low"]) > 1.8 * atr if atr > 0 else False
    flat_market = atr_pct < 0.0006

    return {
        "label": label,
        "df": df,
        "close": close,
        "atr": atr,
        "atr_pct": atr_pct,
        "ema20": float(last["ema20"]),
        "ema50": float(last["ema50"]),
        "ema200": float(last["ema200"]),
        "rsi": float(last["rsi"]),
        "last_high": last_high,
        "last_low": last_low,
        "trend_up": bool(trend_up),
        "trend_down": bool(trend_down),
        "bos_up": bool(bos_up),
        "bos_down": bool(bos_down),
        "bull_sweep": bool(bull_sweep),
        "bear_sweep": bool(bear_sweep),
        "bull_div": bool(bull_div),
        "bear_div": bool(bear_div),
        "bottom_reject": bool(bottom_reject),
        "top_reject": bool(top_reject),
        "huge_candle": bool(huge_candle),
        "flat_market": bool(flat_market),
    }


def build_signal(h4, h1, m15):
    price = m15["close"]
    atr = m15["atr"]

    long_score = 0
    short_score = 0
    long_reasons = []
    short_reasons = []

    if h4["trend_up"]:
        long_score += 18
        long_reasons.append("H4 haussier")
    if h4["trend_down"]:
        short_score += 18
        short_reasons.append("H4 baissier")

    if h1["trend_up"]:
        long_score += 18
        long_reasons.append("H1 haussier")
    if h1["trend_down"]:
        short_score += 18
        short_reasons.append("H1 baissier")

    if price > m15["ema20"] and price > m15["ema50"]:
        long_score += 10
        long_reasons.append("M15 au-dessus EMA20/EMA50")
    if price < m15["ema20"] and price < m15["ema50"]:
        short_score += 10
        short_reasons.append("M15 sous EMA20/EMA50")

    if m15["bos_up"]:
        long_score += 18
        long_reasons.append("cassure structure M15 haussière")
    if m15["bos_down"]:
        short_score += 18
        short_reasons.append("cassure structure M15 baissière")

    if m15["bull_sweep"]:
        long_score += 16
        long_reasons.append("sweep liquidité bas + réintégration")
    if m15["bear_sweep"]:
        short_score += 16
        short_reasons.append("sweep liquidité haut + rejet")

    if m15["bottom_reject"]:
        long_score += 10
        long_reasons.append("mèche basse de rejet")
    if m15["top_reject"]:
        short_score += 10
        short_reasons.append("mèche haute de rejet")

    if m15["bull_div"]:
        long_score += 8
        long_reasons.append("divergence haussière RSI")
    if m15["bear_div"]:
        short_score += 8
        short_reasons.append("divergence baissière RSI")

    if h1["bull_sweep"]:
        long_score += 12
        long_reasons.append("sweep H1 haussier")
    if h1["bear_sweep"]:
        short_score += 12
        short_reasons.append("sweep H1 baissier")

    if h1["bos_up"]:
        long_score += 10
        long_reasons.append("structure H1 confirme hausse")
    if h1["bos_down"]:
        short_score += 10
        short_reasons.append("structure H1 confirme baisse")

    if m15["flat_market"]:
        long_score -= 20
        short_score -= 20

    if m15["huge_candle"]:
        long_score -= 10
        short_score -= 10

    # Deux familles de setup :
    # 1) continuation alignée H4/H1
    # 2) retournement sniper après sweep + cassure M15
    long_continuation = h4["trend_up"] and h1["trend_up"] and m15["bos_up"]
    short_continuation = h4["trend_down"] and h1["trend_down"] and m15["bos_down"]

    long_reversal = (
        (m15["bull_sweep"] or h1["bull_sweep"])
        and m15["bos_up"]
        and (m15["bottom_reject"] or m15["bull_div"])
        and not h4["trend_down"]
    )

    short_reversal = (
        (m15["bear_sweep"] or h1["bear_sweep"])
        and m15["bos_down"]
        and (m15["top_reject"] or m15["bear_div"])
        and not h4["trend_up"]
    )

    long_allowed = long_score >= 74 and (long_continuation or long_reversal)
    short_allowed = short_score >= 74 and (short_continuation or short_reversal)

    if long_allowed and long_score >= short_score:
        side = "LONG"
        setup_type = "Continuation" if long_continuation else "Retournement sniper"
        recent_low = float(m15["df"]["low"].tail(10).min())
        sl = min(recent_low, m15["last_low"]) - 0.20 * atr
        risk = price - sl
        tp1 = price + 2.0 * risk
        tp2 = price + 3.0 * risk
        reasons = long_reasons
        score = long_score

    elif short_allowed and short_score > long_score:
        side = "SHORT"
        setup_type = "Continuation" if short_continuation else "Retournement sniper"
        recent_high = float(m15["df"]["high"].tail(10).max())
        sl = max(recent_high, m15["last_high"]) + 0.20 * atr
        risk = sl - price
        tp1 = price - 2.0 * risk
        tp2 = price - 3.0 * risk
        reasons = short_reasons
        score = short_score

    else:
        return {
            "side": "HOLD",
            "score_long": round(long_score, 1),
            "score_short": round(short_score, 1),
            "reason": "Aucun setup assez fiable selon les filtres V4.",
        }

    if risk <= 0:
        return {"side": "HOLD", "reason": "Risque invalide."}

    risk_pct = risk / price

    if risk_pct < 0.0005:
        return {"side": "HOLD", "reason": "Stop trop serré, bruit de marché probable."}

    if risk_pct > 0.009:
        return {"side": "HOLD", "reason": "Stop trop large, RR réel défavorable."}

    return {
        "side": side,
        "setup_type": setup_type,
        "score": round(score, 1),
        "price": round(price, 2),
        "entry": round(price, 2),
        "sl": round(sl, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2, 2),
        "risk": round(risk, 2),
        "rr1": 2.0,
        "rr2": 3.0,
        "reasons": reasons[:8],
        "score_long": round(long_score, 1),
        "score_short": round(short_score, 1),
        "h4": h4,
        "h1": h1,
        "m15": m15,
    }


def format_message(signal):
    side = signal["side"]
    emoji = "🟢" if side == "LONG" else "🔴"

    reasons = "\n".join([f"• {r}" for r in signal["reasons"]])

    return f"""<b>{emoji} GROK GOLD V4 — {side} VALIDÉ</b>

<b>Actif :</b> {SYMBOL}
<b>Type :</b> {signal["setup_type"]}
<b>Score qualité :</b> {signal["score"]}/100

<b>Entrée zone :</b> {signal["entry"]}
<b>Stop loss :</b> {signal["sl"]}
<b>TP1 :</b> {signal["tp1"]} | RR 1:2
<b>TP2 :</b> {signal["tp2"]} | RR 1:3
<b>Risque prix :</b> {signal["risk"]}

<b>Raisons :</b>
{reasons}

<b>Structure :</b>
H4 close {round(signal["h4"]["close"], 2)}
H1 close {round(signal["h1"]["close"], 2)}
M15 close {round(signal["m15"]["close"], 2)}

<i>Signal technique automatique. Vérifie toujours le spread, la taille de position et les grosses news USD avant d’entrer.</i>"""


def main():
    print(f"GROK GOLD BOT V4 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Symbol: {SYMBOL}")

    frames = fetch_frames()

    m15 = get_tf(frames, "M15")
    h1 = get_tf(frames, "H1")
    h4 = get_tf(frames, "H4")

    if m15 is None or h1 is None or h4 is None:
        raise RuntimeError("Données insuffisantes M15/H1/H4.")

    h4a = analyze_tf(h4, "H4")
    h1a = analyze_tf(h1, "H1")
    m15a = analyze_tf(m15, "M15")

    signal = build_signal(h4a, h1a, m15a)

    if signal["side"] == "HOLD":
        print(
            f"Prix: {m15a['close']:.2f} | Action: HOLD | "
            f"LongScore: {signal.get('score_long')} | ShortScore: {signal.get('score_short')} | "
            f"Reason: {signal.get('reason')}"
        )
        return

    print(
        f"Prix: {signal['price']} | Action: {signal['side']} | "
        f"Score: {signal['score']} | Entry: {signal['entry']} | SL: {signal['sl']} | TP1: {signal['tp1']}"
    )

    msg = format_message(signal)
    send_telegram(msg)


if __name__ == "__main__":
    main()
