from datetime import datetime
from urllib.parse import quote
import requests
import pandas as pd
import numpy as np

from run_once_v41 import (
    SYMBOL,
    send_telegram,
    fetch_frames,
    get_tf,
    analyze,
    macro_filter,
    score_signal,
    format_trade,
    print_hold,
    cfg,
    normalize_df,
    add_indicators,
    wick_info,
)

from run_once_v42 import detect_formation, format_formation


V43_IMPULSE_SCORE = float(cfg("V43_IMPULSE_SCORE", "68"))


def fetch_m5_twelvedata():
    api_key = cfg("TWELVEDATA_API_KEY", "")
    symbol = cfg("TWELVEDATA_SYMBOL", "XAU/USD")

    if not api_key:
        print("[M5] Pas de clé Twelve Data.")
        return None

    try:
        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol": symbol,
            "interval": "5min",
            "outputsize": 140,
            "apikey": api_key,
        }

        r = requests.get(url, params=params, timeout=8)
        js = r.json()

        if "values" not in js:
            print(f"[M5] Réponse Twelve Data invalide: {js.get('message', js)}")
            return None

        df = pd.DataFrame(js["values"])
        df = df.rename(columns={"datetime": "datetime"})
        df = df.iloc[::-1].reset_index(drop=True)

        return normalize_df(df)

    except Exception as e:
        print(f"[M5] Erreur fetch Twelve Data: {e}")
        return None


def enrich(df):
    if df is None or len(df) < 40:
        return None
    return add_indicators(df.copy())


def impulse_engine(df, tf_label, m15, h1, h4, macro):
    df = enrich(df)

    if df is None or len(df) < 40:
        return {
            "alert": False,
            "reason": "Données insuffisantes pour impulse engine.",
        }

    last = df.iloc[-1]
    prev = df.iloc[-2]

    close = float(last["close"])
    open_ = float(last["open"])
    high = float(last["high"])
    low = float(last["low"])
    atr = max(float(last["atr"]), 0.0001)

    rng = high - low
    body = close - open_
    body_abs = abs(body)
    direction = "LONG" if body > 0 else "SHORT"

    upper, lower, body_ratio = wick_info(last)

    prev_20_high = float(df["high"].shift(1).rolling(20).max().iloc[-1])
    prev_20_low = float(df["low"].shift(1).rolling(20).min().iloc[-1])

    prev_50_high = float(df["high"].shift(1).rolling(50).max().iloc[-1])
    prev_50_low = float(df["low"].shift(1).rolling(50).min().iloc[-1])

    range_ratio = rng / atr
    body_atr = body_abs / atr

    move_3 = close - float(df["close"].iloc[-4])
    move_6 = close - float(df["close"].iloc[-7])
    speed_3 = move_3 / atr
    speed_6 = move_6 / atr

    last_3 = df.tail(3)
    streak_up = bool((last_3["close"] > last_3["open"]).sum() >= 3)
    streak_down = bool((last_3["close"] < last_3["open"]).sum() >= 3)

    breakout_up = bool(close > prev_20_high)
    breakout_down = bool(close < prev_20_low)

    major_breakout_up = bool(close > prev_50_high)
    major_breakout_down = bool(close < prev_50_low)

    close_position = (close - low) / max(rng, 0.0001)

    atr_now = float(df["atr"].tail(5).mean())
    atr_base = float(df["atr"].tail(60).mean())
    atr_expansion = atr_now / max(atr_base, 0.0001)

    day_high = float(m15["df"]["high"].tail(96).max())
    day_low = float(m15["df"]["low"].tail(96).min())

    recent_8h_high = float(m15["df"]["high"].tail(32).max())
    recent_8h_low = float(m15["df"]["low"].tail(32).min())

    near_day_high = bool((day_high - close) <= 1.0 * atr)
    near_day_low = bool((close - day_low) <= 1.0 * atr)

    long_score = 0
    short_score = 0
    long_reasons = []
    short_reasons = []

    if range_ratio >= 1.45 and body > 0 and close_position >= 0.70:
        long_score += 18
        long_reasons.append("bougie acheteuse anormale vs ATR")

    if range_ratio >= 1.45 and body < 0 and close_position <= 0.30:
        short_score += 18
        short_reasons.append("bougie vendeuse anormale vs ATR")

    if speed_3 >= 1.05:
        long_score += 16
        long_reasons.append("accélération haussière rapide")

    if speed_3 <= -1.05:
        short_score += 16
        short_reasons.append("accélération baissière rapide")

    if speed_6 >= 1.60:
        long_score += 12
        long_reasons.append("pression acheteuse persistante")

    if speed_6 <= -1.60:
        short_score += 12
        short_reasons.append("pression vendeuse persistante")

    if breakout_up:
        long_score += 16
        long_reasons.append("cassure high court terme")

    if breakout_down:
        short_score += 16
        short_reasons.append("cassure low court terme")

    if major_breakout_up:
        long_score += 12
        long_reasons.append("cassure high majeur récent")

    if major_breakout_down:
        short_score += 12
        short_reasons.append("cassure low majeur récent")

    if streak_up:
        long_score += 8
        long_reasons.append("3 bougies vertes consécutives")

    if streak_down:
        short_score += 8
        short_reasons.append("3 bougies rouges consécutives")

    if atr_expansion >= 1.22:
        long_score += 6
        short_score += 6

    if h1["trend_up"] or h1["soft_up"]:
        long_score += 10
        long_reasons.append("biais H1 favorable hausse")

    if h1["trend_down"] or h1["soft_down"]:
        short_score += 10
        short_reasons.append("biais H1 favorable baisse")

    if h4["trend_up"]:
        long_score += 8
        long_reasons.append("H4 soutient la hausse")

    if h4["trend_down"]:
        short_score += 8
        short_reasons.append("H4 soutient la baisse")

    if macro["dxy_bias"] == "DOWN":
        long_score += 10
        long_reasons.append("DXY baisse, favorable or")
    elif macro["dxy_bias"] == "UP":
        short_score += 10
        short_reasons.append("DXY monte, pression sur l’or")

    if macro["tnx_bias"] == "DOWN":
        long_score += 8
        long_reasons.append("US10Y baisse, favorable or")
    elif macro["tnx_bias"] == "UP":
        short_score += 8
        short_reasons.append("US10Y monte, pression sur l’or")

    if near_day_high and direction == "LONG":
        long_score += 5
        long_reasons.append("attaque du high récent")

    if near_day_low and direction == "SHORT":
        short_score += 5
        short_reasons.append("attaque du low récent")

    if upper >= 0.45 and direction == "LONG":
        long_score -= 10
        long_reasons.append("attention mèche haute")

    if lower >= 0.45 and direction == "SHORT":
        short_score -= 10
        short_reasons.append("attention mèche basse")

    if range_ratio < 0.65 and atr_expansion < 0.95:
        regime = "RANGE / CALME"
    elif atr_expansion >= 1.25 and max(long_score, short_score) >= 55:
        regime = "EXPANSION / IMPULSION"
    elif h1["trend_up"] or h1["trend_down"]:
        regime = "TENDANCE"
    else:
        regime = "CHOP / MIXTE"

    best_side = "LONG" if long_score >= short_score else "SHORT"
    best_score = max(long_score, short_score)

    macro_conflict = False

    if best_side == "LONG" and macro["dxy_bias"] == "UP" and macro["tnx_bias"] == "UP":
        macro_conflict = True

    if best_side == "SHORT" and macro["dxy_bias"] == "DOWN" and macro["tnx_bias"] == "DOWN":
        macro_conflict = True

    violent_condition = (
        range_ratio >= 1.45
        or abs(speed_3) >= 1.05
        or abs(speed_6) >= 1.60
        or major_breakout_up
        or major_breakout_down
    )

    alert = bool(best_score >= V43_IMPULSE_SCORE and violent_condition)

    if macro_conflict and best_score < V43_IMPULSE_SCORE + 12:
        alert = False

    return {
        "alert": alert,
        "side": best_side,
        "score": round(best_score, 1),
        "long_score": round(long_score, 1),
        "short_score": round(short_score, 1),
        "price": round(close, 2),
        "tf": tf_label,
        "regime": regime,
        "range_ratio": round(range_ratio, 2),
        "body_atr": round(body_atr, 2),
        "speed_3": round(speed_3, 2),
        "speed_6": round(speed_6, 2),
        "atr_expansion": round(atr_expansion, 2),
        "breakout_up": breakout_up,
        "breakout_down": breakout_down,
        "major_breakout_up": major_breakout_up,
        "major_breakout_down": major_breakout_down,
        "prev_20_high": round(prev_20_high, 2),
        "prev_20_low": round(prev_20_low, 2),
        "prev_50_high": round(prev_50_high, 2),
        "prev_50_low": round(prev_50_low, 2),
        "day_high": round(day_high, 2),
        "day_low": round(day_low, 2),
        "recent_8h_high": round(recent_8h_high, 2),
        "recent_8h_low": round(recent_8h_low, 2),
        "macro_conflict": macro_conflict,
        "macro": macro,
        "reasons": (long_reasons if best_side == "LONG" else short_reasons)[:9],
    }


def format_impulse(imp):
    emoji = "🚨"
    side = imp["side"]

    if side == "LONG":
        confirmation = imp["prev_20_high"]
        retest = imp["prev_20_high"]
        invalidation = imp["recent_8h_low"]
        title = "IMPULSE LONG DÉTECTÉE"
        trigger = f"Surveiller maintien au-dessus de {confirmation} ou retest propre de {retest}"
    else:
        confirmation = imp["prev_20_low"]
        retest = imp["prev_20_low"]
        invalidation = imp["recent_8h_high"]
        title = "IMPULSE SHORT DÉTECTÉE"
        trigger = f"Surveiller maintien sous {confirmation} ou retest propre de {retest}"

    reasons = "\n".join([f"• {r}" for r in imp["reasons"]]) or "• Impulsion détectée"

    conflict = ""
    if imp["macro_conflict"]:
        conflict = "\n⚠️ <b>Attention :</b> macro partiellement contradictoire."

    return f"""<b>{emoji} GROK GOLD V4.3 — {title}</b>

<b>Actif :</b> {SYMBOL}
<b>Prix :</b> {imp['price']}
<b>Timeframe radar :</b> {imp['tf']}
<b>Régime :</b> {imp['regime']}
<b>Score impulsion :</b> {imp['score']}/100

<b>Ce n’est PAS encore forcément un trade validé.</b>
Le bot détecte un changement brutal de marché.

<b>Plan à surveiller :</b>
{trigger}

<b>Invalidation scénario :</b>
{invalidation}

<b>Puissance mouvement :</b>
Bougie vs ATR : x{imp['range_ratio']}
Vitesse 3 bougies : {imp['speed_3']} ATR
Expansion volatilité : x{imp['atr_expansion']}

<b>Macro :</b>
DXY : {imp['macro']['dxy_bias']}
US10Y : {imp['macro']['tnx_bias']}
{conflict}

<b>Raisons :</b>
{reasons}

<i>Radar impulsion. Attendre confirmation/retest si tu veux éviter d’acheter le top ou shorter le bottom.</i>"""


def main():
    print(f"GROK GOLD BOT V4.3 RADAR PRO - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Symbol: {SYMBOL}")
    print(f"Impulse score min: {V43_IMPULSE_SCORE}")

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

    if sig["side"] != "HOLD":
        print(
            f"TRADE VALIDÉ | Prix: {sig['price']} | Action: {sig['side']} | "
            f"Score: {sig['score']} | Entry: {sig['entry']} | SL: {sig['sl']} | TP1: {sig['tp1']}"
        )
        send_telegram(format_trade(sig))
        return

    print_hold(sig)

    m5 = fetch_m5_twelvedata()

    if m5 is not None:
        imp = impulse_engine(m5, "M5", m15a, h1a, h4a, macro)
    else:
        print("[M5] Fallback impulse engine sur M15.")
        imp = impulse_engine(m15a["df"], "M15", m15a, h1a, h4a, macro)

    print(
        f"Impulse | Alert={imp.get('alert')} | Side={imp.get('side')} | "
        f"Score={imp.get('score')} | Regime={imp.get('regime')} | "
        f"RangeATR={imp.get('range_ratio')} | Speed3={imp.get('speed_3')} | "
        f"ATRExpansion={imp.get('atr_expansion')}"
    )

    if imp.get("alert"):
        send_telegram(format_impulse(imp))
        return

    formation = detect_formation(sig)

    if formation:
        print(f"Formation détectée: {formation}")
        send_telegram(format_formation(formation, sig))
    else:
        print("Aucune formation ou impulsion assez propre pour alerte Telegram.")


if __name__ == "__main__":
    main()
