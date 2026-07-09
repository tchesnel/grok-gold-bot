"""
GROK TRADER WAR MACHINE - main_live.py
V3 XAUUSD PRO

- XAUUSD via YAHOO ou MT5
- Multi-timeframe 15m / 1h / 4h
- Macro : DXY / US10Y / US2Y
- Géopolitique : GDELT
- Bougies clôturées uniquement
- Journal CSV
- Telegram
- Aucun ordre automatique
"""

import time
import json
import csv
import argparse
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import pandas as pd
import numpy as np
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


PRIMARY_TIMEFRAME = "1h"
LOOP_SLEEP_SECONDS = 300
SIGNAL_COOLDOWN_SECONDS = 1800

MIN_DECISION_CONFIDENCE = 0.78
MIN_LONG_GLOBAL_SCORE = 68
MIN_SHORT_GLOBAL_SCORE = 32

LATEST_SIGNAL_FILE = Path("latest_signal.json")
CONTROL_FILE = Path("bot_control.json")
SIGNAL_LOG_FILE = Path("signals_log.csv")
ANALYSIS_LOG_FILE = Path("analysis_log.csv")

BACKGROUND_EXECUTOR = ThreadPoolExecutor(max_workers=4)


# ==========================================================
# OUTILS
# ==========================================================

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_symbol_display() -> str:
    return getattr(settings, "SYMBOL", "XAUUSD") or "XAUUSD"


def safe_fetch_snapshot(name: str, func, fallback: dict, timeout_seconds: int = 6) -> dict:
    """
    Exécute macro/géo avec timeout dur.
    Si la source externe est lente, le bot continue avec le cache précédent.
    """
    future = BACKGROUND_EXECUTOR.submit(func)

    try:
        result = future.result(timeout=timeout_seconds)

        if isinstance(result, dict):
            return result

        print(f"[{name}] Réponse invalide, fallback utilisé.")
        return fallback

    except TimeoutError:
        print(f"[{name}] Timeout après {timeout_seconds}s → fallback utilisé.")
        return fallback

    except Exception as e:
        print(f"[{name}] Erreur: {type(e).__name__} : {e} → fallback utilisé.")
        return fallback


def get_data_source() -> str:
    return getattr(settings, "DATA_SOURCE", "YAHOO")


def clean_md(text) -> str:
    """
    Nettoyage simple pour éviter que Telegram Markdown plante avec certains titres de news.
    """
    if text is None:
        return "N/A"

    text = str(text)
    for ch in ["*", "_", "`", "[", "]"]:
        text = text.replace(ch, "")
    return text.strip()


def is_paused() -> bool:
    if not CONTROL_FILE.exists():
        return False

    try:
        with open(CONTROL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        return bool(data.get("paused", False))
    except Exception:
        return False


# ==========================================================
# TELEGRAM
# ==========================================================

def send_telegram_message(message: str):
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
    chat_id = getattr(settings, "TELEGRAM_CHAT_ID", None)

    if not token:
        print("[Telegram] Aucun TELEGRAM_BOT_TOKEN configuré.")
        return False

    if not chat_id:
        print("[Telegram] Aucun TELEGRAM_CHAT_ID configuré.")
        return False

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"

        data = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }

        r = requests.post(url, data=data, timeout=20)

        if r.status_code == 200:
            print("[Telegram] Message envoyé ✓")
            return True

        print(f"[Telegram Error] {r.status_code} | {r.text}")
        return False

    except Exception as e:
        print(f"[Telegram Error] {type(e).__name__} : {e}")
        return False


def send_startup_message(symbol: str, mode: str):
    msg = (
        f"✅ *GROK Gold Bot V3 démarré*\n\n"
        f"Mode: `{mode.upper()}`\n"
        f"Data source: `{get_data_source()}`\n"
        f"Actif: `{symbol}`\n"
        f"Analyse: `15m / 1h / 4h`\n"
        f"Macro: `DXY / US10Y / US2Y`\n"
        f"Géo: `GDELT`\n\n"
        f"Alertes uniquement. Aucun ordre automatique."
    )

    send_telegram_message(msg)


def send_test_trade_alert(symbol: str):
    msg = (
        f"🧪 *TEST TRADE ALERT V3*\n\n"
        f"Actif: `{symbol}`\n"
        f"Signal: `LONG`\n"
        f"Score global: `76/100`\n"
        f"Entrée approx: `4120.00`\n"
        f"SL: `4095.00`\n"
        f"TP1: `4150.00`\n"
        f"TP2: `4185.00`\n\n"
        f"✅ Si tu reçois ce message, le bot V3 est connecté."
    )

    send_telegram_message(msg)


# ==========================================================
# INDICATEURS CONTEXTE
# ==========================================================

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


# ==========================================================
# SIGNAL
# ==========================================================

def is_strong_signal(decision, risk_info) -> bool:
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

    geo_titles = geo_info.get("top_titles", [])
    geo_text = "\n".join([f"• {clean_md(t)}" for t in geo_titles[:3]]) if geo_titles else "Aucun titre fort."

    macro_details = macro_info.get("details", {})

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


# ==========================================================
# SAVE / LOG
# ==========================================================

def save_latest_signal(
    symbol,
    decision,
    regime_info,
    sentiment_info,
    technical_info,
    risk_info,
    macro_info,
    geo_info,
    current_price,
    atr_value,
    mode
):
    data = {
        "time": now_str(),
        "symbol": symbol,
        "mode": mode,
        "data_source": get_data_source(),
        "action": decision.get("action"),
        "price": round(float(current_price), 2),
        "atr": round(float(atr_value), 4),
        "confidence": round(float(decision.get("confidence", 0)) * 100, 0),
        "score": decision.get("composite_score", 0),
        "global_score": decision.get("global_score", 50),
        "regime": regime_info.get("regime"),
        "sentiment_score": sentiment_info.get("sentiment_score"),
        "macro": macro_info,
        "geo": geo_info,
        "technical_trend": technical_info.get("trend"),
        "risk_pct": risk_info.get("risk_pct", 0),
        "risk_reason": risk_info.get("reason"),
        "order_sent": False,
        "order": None,
        "explanation": decision.get("explanation", "")
    }

    with open(LATEST_SIGNAL_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"[STATE] Dernier signal sauvegardé dans {LATEST_SIGNAL_FILE}")


def log_signal(symbol, decision, current_price, risk_info, macro_info, geo_info):
    file_exists = SIGNAL_LOG_FILE.exists()

    with open(SIGNAL_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        if not file_exists:
            writer.writerow([
                "time",
                "symbol",
                "action",
                "price",
                "global_score",
                "confidence",
                "score",
                "risk_pct",
                "macro_score",
                "geo_score",
                "explanation"
            ])

        writer.writerow([
            now_str(),
            symbol,
            decision.get("action"),
            round(float(current_price), 2),
            decision.get("global_score"),
            round(float(decision.get("confidence", 0)) * 100, 0),
            decision.get("composite_score"),
            risk_info.get("risk_pct"),
            macro_info.get("score"),
            geo_info.get("score"),
            decision.get("explanation")
        ])



def log_analysis(
    symbol,
    current_price,
    decision,
    regime_info,
    sentiment_info,
    technical_info,
    risk_info,
    macro_info,
    geo_info,
    atr_value
):
    """
    Enregistre toutes les analyses, même HOLD.
    C'est indispensable pour savoir si le bot est vraiment bon.
    """
    file_exists = ANALYSIS_LOG_FILE.exists()

    h4 = technical_info.get("h4", {})
    h1 = technical_info.get("h1", {})
    m15 = technical_info.get("m15", {})

    with open(ANALYSIS_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        if not file_exists:
            writer.writerow([
                "time",
                "symbol",
                "price",
                "action",
                "global_score",
                "raw_score",
                "confidence",
                "regime",
                "regime_confidence",
                "sentiment_score",
                "technical_score",
                "macro_score",
                "geo_score",
                "h4_trend",
                "h1_trend",
                "m15_trend",
                "h4_rsi",
                "h1_rsi",
                "m15_rsi",
                "atr",
                "risk_pct",
                "explanation"
            ])

        writer.writerow([
            now_str(),
            symbol,
            round(float(current_price), 2),
            decision.get("action"),
            decision.get("global_score"),
            decision.get("composite_score"),
            round(float(decision.get("confidence", 0)) * 100, 0),
            regime_info.get("regime"),
            regime_info.get("confidence"),
            sentiment_info.get("sentiment_score"),
            technical_info.get("technical_score"),
            macro_info.get("score"),
            geo_info.get("score"),
            h4.get("trend"),
            h1.get("trend"),
            m15.get("trend"),
            h4.get("rsi"),
            h1.get("rsi"),
            m15.get("rsi"),
            round(float(atr_value), 4),
            risk_info.get("risk_pct"),
            decision.get("explanation")
        ])



# ==========================================================
# MAIN
# ==========================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["paper", "live"], default="live")
    parser.add_argument("--test-alert", action="store_true")
    args = parser.parse_args()

    symbol = get_symbol_display()

    print("=" * 70)
    print(f"GROK TRADER WAR MACHINE V3 - MODE : {args.mode.upper()}")
    print("=" * 70)
    print("[INFO] Version XAUUSD macro + géopolitique + technique.")
    print("[INFO] Alertes uniquement. Aucun ordre automatique.")
    print(f"[INFO] Data source : {get_data_source()}")
    print(f"[INFO] Symbole : {symbol}")
    print("[INFO] Timeframes : 15m / 1h / 4h")
    print(f"[INFO] Analyse toutes les {LOOP_SLEEP_SECONDS} secondes.")
    print(f"[INFO] Cooldown : {SIGNAL_COOLDOWN_SECONDS // 60} minutes.")
    print("[INFO] Bot lancé. Ctrl + C pour arrêter.\n")

    if args.test_alert:
        send_test_trade_alert(symbol)
        return

    send_startup_message(symbol, args.mode)

    risk_engine = RiskEngine(settings)
    risk_agent = RiskPortfolioAgent(risk_engine)
    coordinator = StrategistCoordinator()

    regime_agent = DataRegimeAgent()
    sentiment_agent = SentimentNarrativeAgent()
    technical_agent = TechnicalDeepAgent()

    last_signal_key = None
    last_signal_time = 0

    macro_cache = {"score": 0.0, "narrative": "Macro neutre", "details": {}}
    geo_cache = {"score": 0.0, "narrative": "Géopolitique neutre", "article_count": 0, "top_titles": []}

    last_macro_fetch = 0
    last_geo_fetch = 0

    try:
        while True:
            try:
                if is_paused():
                    print(f"[{now_str()}] Bot en pause via Telegram /pause.")
                    time.sleep(LOOP_SLEEP_SECONDS)
                    continue

                print("[STEP] Récupération XAU/USD 15m / 1h / 4h...")
                data = fetch_all_timeframes()
                print("[STEP] Données XAU/USD OK")

                if any(len(df) < 60 for df in data.values()):
                    print("[WARN] Pas assez de données multi-timeframe.")
                    time.sleep(LOOP_SLEEP_SECONDS)
                    continue

                h1_df = data["1h"]
                current_price = float(h1_df["close"].iloc[-1])
                atr_value = calculate_atr_simple(h1_df)

                if np.isnan(atr_value) or atr_value <= 0:
                    print("[WARN] ATR invalide.")
                    time.sleep(LOOP_SLEEP_SECONDS)
                    continue

                now_ts = time.time()

                # Macro toutes les 5 minutes
                if now_ts - last_macro_fetch > 900:
                    print("[STEP] Récupération macro DXY / US10Y / US2Y...")
                    macro_cache = safe_fetch_snapshot(
                        name="MACRO",
                        func=get_macro_snapshot,
                        fallback=macro_cache,
                        timeout_seconds=6
                    )
                    print("[STEP] Macro OK ou fallback")
                    last_macro_fetch = now_ts

                # Géopolitique toutes les 10 minutes
                if now_ts - last_geo_fetch > 1800:
                    print("[STEP] Récupération géopolitique GDELT...")
                    geo_cache = safe_fetch_snapshot(
                        name="GEO",
                        func=get_geo_snapshot,
                        fallback=geo_cache,
                        timeout_seconds=6
                    )
                    print("[STEP] Géopolitique OK ou fallback")
                    last_geo_fetch = now_ts

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
                    macro=macro_cache,
                    geo=geo_cache
                )

                print(
                    f"[{now_str()}] "
                    f"Prix: {current_price:.2f} | "
                    f"Action: {decision['action']} | "
                    f"Global: {decision.get('global_score')}/100 | "
                    f"Score: {decision.get('composite_score', 0):+.2f} | "
                    f"H4: {technical_info['h4']['trend']} | "
                    f"H1: {technical_info['h1']['trend']} | "
                    f"M15: {technical_info['m15']['trend']} | "
                    f"Macro: {macro_cache.get('score'):+.2f} | "
                    f"Geo: {geo_cache.get('score'):+.2f}"
                )

                log_analysis(
                    symbol=symbol,
                    current_price=current_price,
                    decision=decision,
                    regime_info=regime_info,
                    sentiment_info=sentiment_info,
                    technical_info=technical_info,
                    risk_info=risk_info,
                    macro_info=macro_cache,
                    geo_info=geo_cache,
                    atr_value=atr_value
                )

                strong_signal = is_strong_signal(decision, risk_info)

                if strong_signal:
                    candle_time = h1_df.index[-1].strftime("%Y-%m-%d %H:%M")
                    signal_key = f"{symbol}_{decision['action']}_{candle_time}"

                    can_send_signal = (
                        signal_key != last_signal_key
                        and now_ts - last_signal_time > SIGNAL_COOLDOWN_SECONDS
                    )

                    save_latest_signal(
                        symbol=symbol,
                        decision=decision,
                        regime_info=regime_info,
                        sentiment_info=sentiment_info,
                        technical_info=technical_info,
                        risk_info=risk_info,
                        macro_info=macro_cache,
                        geo_info=geo_cache,
                        current_price=current_price,
                        atr_value=atr_value,
                        mode=args.mode
                    )

                    if can_send_signal:
                        msg = build_signal_message(
                            symbol=symbol,
                            decision=decision,
                            regime_info=regime_info,
                            sentiment_info=sentiment_info,
                            technical_info=technical_info,
                            risk_info=risk_info,
                            macro_info=macro_cache,
                            geo_info=geo_cache,
                            current_price=current_price,
                            atr_value=atr_value
                        )

                        send_telegram_message(msg)
                        log_signal(symbol, decision, current_price, risk_info, macro_cache, geo_cache)

                        last_signal_key = signal_key
                        last_signal_time = now_ts

                    else:
                        print("[ANTI-SPAM] Signal fort déjà envoyé sur cette bougie ou cooldown actif.")

                time.sleep(LOOP_SLEEP_SECONDS)

            except Exception as e:
                error_text = str(e)

                print(f"[ERREUR BOUCLE] {type(e).__name__} : {e}")

                if "429" in error_text or "Too Many Requests" in error_text:
                    print("[YAHOO LIMIT] Yahoo bloque temporairement les requêtes.")
                    print("[YAHOO LIMIT] Pause automatique 30 minutes pour éviter d'aggraver le blocage.")
                    time.sleep(1800)
                else:
                    time.sleep(LOOP_SLEEP_SECONDS)

    except KeyboardInterrupt:
        print("\nBot arrêté proprement.")


if __name__ == "__main__":
    main()
