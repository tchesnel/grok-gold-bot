import time
import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import numpy as np
import pandas as pd
import requests

from config import settings
from risk_engine import RiskEngine
from mt5_data import fetch_all_timeframes
from macro_engine import get_macro_snapshot
from geo_news_engine import get_geo_snapshot
from agents import (
    DataRegimeAgent,
    SentimentNarrativeAgent,
    TechnicalDeepAgent,
    RiskPortfolioAgent,
    StrategistCoordinator
)


MIN_DECISION_CONFIDENCE = 0.78
MIN_LONG_GLOBAL_SCORE = 68
MIN_SHORT_GLOBAL_SCORE = 32


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_md(text):
    if text is None:
        return "N/A"
    text = str(text)
    for ch in ["*", "_", "`", "[", "]"]:
        text = text.replace(ch, "")
    return text.strip()


def safe_fetch_snapshot(name, func, fallback, timeout_seconds=6):
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func)
        try:
            result = future.result(timeout=timeout_seconds)
            if isinstance(result, dict):
                return result
            print(f"[{name}] Réponse invalide, fallback.")
            return fallback
        except TimeoutError:
            print(f"[{name}] Timeout {timeout_seconds}s, fallback.")
            return fallback
        except Exception as e:
            print(f"[{name}] Erreur {type(e).__name__}: {e}, fallback.")
            return fallback


def send_telegram_message(message: str):
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID

    if not token or not chat_id:
        print("[Telegram] Token ou chat_id manquant.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    data = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }

    try:
        r = requests.post(url, data=data, timeout=20)
        if r.status_code == 200:
            print("[Telegram] Message envoyé.")
            return True

        print(f"[Telegram Error] {r.status_code} | {r.text}")
        return False

    except Exception as e:
        print(f"[Telegram Error] {type(e).__name__}: {e}")
        return False


def calculate_atr_simple(df: pd.DataFrame, period: int = 14) -> float:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()

    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    value = tr.rolling(period).mean().iloc[-1]

    return float(value)


def build_market_context(df: pd.DataFrame) -> dict:
    close = df["close"]
    volume = df["volume"]

    momentum = (close.iloc[-1] / close.iloc[-4] - 1) * 100 if len(close) >= 4 else 0

    vol_mean = volume.tail(30).mean()
    vol_std = volume.tail(30).std()

    if vol_std and vol_std > 0:
        volume_z = (volume.iloc[-1] - vol_mean) / vol_std
    else:
        volume_z = 0

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0).rolling(14).mean())
    rs = gain / loss.replace(0, np.nan)
    rsi_value = (100 - (100 / (1 + rs))).fillna(50).iloc[-1]

    return {
        "momentum": round(float(momentum), 3),
        "volume_z": round(float(volume_z), 3),
        "rsi": round(float(rsi_value), 1)
    }


def is_strong_signal(decision, risk_info):
    action = decision.get("action")
    confidence = decision.get("confidence", 0)
    global_score = decision.get("global_score", 50)

    if not risk_info.get("approved", False):
        return False

    if action not in ["LONG", "SHORT"]:
        return False

    if confidence < MIN_DECISION_CONFIDENCE:
        return False

    if action == "LONG" and global_score < MIN_LONG_GLOBAL_SCORE:
        return False

    if action == "SHORT" and global_score > MIN_SHORT_GLOBAL_SCORE:
        return False

    return True


def build_signal_message(
    symbol,
    decision,
    regime_info,
    sentiment_info,
    technical_info,
    risk_info,
    macro_info,
    geo_info,
    current_price,
    atr_value
):
    action = decision["action"]
    entry = round(float(current_price), 2)

    if action == "LONG":
        sl = round(float(current_price - atr_value * 1.6), 2)
        tp1 = round(float(current_price + atr_value * 1.8), 2)
        tp2 = round(float(current_price + atr_value * 3.0), 2)
    else:
        sl = round(float(current_price + atr_value * 1.6), 2)
        tp1 = round(float(current_price - atr_value * 1.8), 2)
        tp2 = round(float(current_price - atr_value * 3.0), 2)

    rr1 = abs(tp1 - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
    rr2 = abs(tp2 - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0

    h4 = technical_info["h4"]
    h1 = technical_info["h1"]
    m15 = technical_info["m15"]

    macro_details = macro_info.get("details", {})
    geo_titles = geo_info.get("top_titles", [])
    geo_text = "\n".join([f"• {clean_md(t)}" for t in geo_titles[:3]]) if geo_titles else "Aucun titre fort."

    msg = (
        f"🤖 *GROK GOLD V3 - TRADE ALERT*\n\n"
        f"*Actif:* `{symbol}`\n"
        f"*Signal:* `{action}`\n"
        f"*Score global:* `{decision.get('global_score')}/100`\n"
        f"*Confiance:* `{decision.get('confidence', 0) * 100:.0f}%`\n"
        f"*Score brut:* `{decision.get('composite_score', 0):+.2f}`\n\n"

        f"*Entrée approx:* `{entry}`\n"
        f"*Stop Loss:* `{sl}`\n"
        f"*TP1:* `{tp1}` | RR `{rr1:.2f}`\n"
        f"*TP2:* `{tp2}` | RR `{rr2:.2f}`\n\n"

        f"*Technique MTF:*\n"
        f"H4: `{h4['trend']}` | RSI `{h4['rsi']}`\n"
        f"H1: `{h1['trend']}` | RSI `{h1['rsi']}`\n"
        f"M15: `{m15['trend']}` | RSI `{m15['rsi']}`\n\n"

        f"*Macro:*\n"
        f"`{clean_md(macro_info.get('narrative'))}` | Score `{macro_info.get('score'):+.2f}`\n"
        f"DXY 24h: `{macro_details.get('dxy_24h_pct', 'N/A')}%`\n"
        f"US10Y: `{macro_details.get('us10y_change_bps', 'N/A')} bps`\n"
        f"US2Y: `{macro_details.get('us2y_change_bps', 'N/A')} bps`\n\n"

        f"*Géopolitique:*\n"
        f"`{clean_md(geo_info.get('narrative'))}` | Score `{geo_info.get('score'):+.2f}`\n"
        f"Articles: `{geo_info.get('article_count')}`\n"
        f"{geo_text}\n\n"

        f"*Régime:* `{regime_info.get('regime')}` "
        f"({regime_info.get('confidence', 0) * 100:.0f}%)\n"
        f"*Risk théorique:* `{risk_info.get('risk_pct', 0)}%`\n\n"

        f"*Explication:*\n{clean_md(decision.get('explanation'))}\n\n"
        f"⚠️ Aucun ordre automatique. Tu dois valider la zone avant d’entrer."
    )

    return msg


def main():
    symbol = settings.SYMBOL or "XAUUSD"

    print("=" * 70)
    print(f"GROK BOT ONE-SHOT - {now_str()}")
    print("=" * 70)
    print("Data source:", settings.DATA_SOURCE)
    print("Symbol:", symbol)

    data = fetch_all_timeframes()

    if any(len(df) < 60 for df in data.values()):
        print("[STOP] Pas assez de données multi-timeframe.")
        return

    h1_df = data["1h"]
    current_price = float(h1_df["close"].iloc[-1])
    atr_value = calculate_atr_simple(h1_df)

    if np.isnan(atr_value) or atr_value <= 0:
        print("[STOP] ATR invalide.")
        return

    macro_fallback = {"score": 0.0, "narrative": "Macro neutre", "details": {}}
    geo_fallback = {"score": 0.0, "narrative": "Géopolitique neutre", "article_count": 0, "top_titles": []}

    macro_info = safe_fetch_snapshot("MACRO", get_macro_snapshot, macro_fallback, timeout_seconds=6)
    geo_info = safe_fetch_snapshot("GEO", get_geo_snapshot, geo_fallback, timeout_seconds=6)

    risk_engine = RiskEngine(settings)
    risk_agent = RiskPortfolioAgent(risk_engine)
    coordinator = StrategistCoordinator()

    regime_agent = DataRegimeAgent()
    sentiment_agent = SentimentNarrativeAgent()
    technical_agent = TechnicalDeepAgent()

    regime_info = regime_agent.analyze(h1_df)
    market_context = build_market_context(h1_df)
    sentiment_info = sentiment_agent.analyze(regime_info["regime"], market_context)
    technical_info = technical_agent.analyze(data)

    risk_info = risk_agent.validate(
        equity=100000,
        proposed_size=12000,
        current_dd=0.015,
        regime=regime_info["regime"],
        price=current_price,
        atr=atr_value
    )

    decision = coordinator.decide(
        regime=regime_info,
        sentiment=sentiment_info,
        technical=technical_info,
        risk=risk_info,
        price=current_price,
        macro=macro_info,
        geo=geo_info
    )

    print(
        f"Prix: {current_price:.2f} | "
        f"Action: {decision['action']} | "
        f"Global: {decision.get('global_score')}/100 | "
        f"Score: {decision.get('composite_score', 0):+.2f} | "
        f"H4: {technical_info['h4']['trend']} | "
        f"H1: {technical_info['h1']['trend']} | "
        f"M15: {technical_info['m15']['trend']} | "
        f"Macro: {macro_info.get('score'):+.2f} | "
        f"Geo: {geo_info.get('score'):+.2f}"
    )

    if is_strong_signal(decision, risk_info):
        msg = build_signal_message(
            symbol=symbol,
            decision=decision,
            regime_info=regime_info,
            sentiment_info=sentiment_info,
            technical_info=technical_info,
            risk_info=risk_info,
            macro_info=macro_info,
            geo_info=geo_info,
            current_price=current_price,
            atr_value=atr_value
        )
        send_telegram_message(msg)
    else:
        print("[INFO] Pas de signal fort. Aucun message Telegram trade envoyé.")


if __name__ == "__main__":
    main()
