"""
GROK TRADER WAR MACHINE - agents.py
V3 compatible macro + géopolitique + XAUUSD
"""

import numpy as np
import pandas as pd
from datetime import datetime
from typing import Dict, Any

from risk_engine import RiskEngine


# ==========================================================
# INDICATEURS
# ==========================================================

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0).rolling(period).mean())
    rs = gain / loss.replace(0, np.nan)
    value = 100 - (100 / (1 + rs))
    return value.fillna(50)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def macd_hist(series: pd.Series) -> pd.Series:
    macd_line = ema(series, 12) - ema(series, 26)
    signal = ema(macd_line, 9)
    return macd_line - signal


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


# ==========================================================
# REGIME
# ==========================================================

class DataRegimeAgent:
    def analyze(self, df: pd.DataFrame) -> Dict[str, Any]:
        close = df["close"]

        if len(df) < 60:
            return {
                "regime": "Unknown",
                "trend_30": 0,
                "volatility": 0,
                "confidence": 0.0
            }

        ret = close.pct_change().dropna()
        vol = ret.tail(30).std() * np.sqrt(252)
        trend_30 = (close.iloc[-1] / close.iloc[-30] - 1) * 100

        ema20_value = ema(close, 20).iloc[-1]
        ema50_value = ema(close, 50).iloc[-1]
        price = close.iloc[-1]

        if price > ema20_value > ema50_value and trend_30 > 0.25:
            regime = "Bull"
        elif price < ema20_value < ema50_value and trend_30 < -0.25:
            regime = "Bear"
        elif vol > 0.30:
            regime = "Volatile"
        else:
            regime = "Sideways"

        trend_strength = min(abs(trend_30) / 2.5, 1.0)
        alignment = 1.0 if price > ema20_value > ema50_value or price < ema20_value < ema50_value else 0.45
        confidence = 0.45 + trend_strength * 0.35 + alignment * 0.20

        return {
            "regime": regime,
            "trend_30": round(float(trend_30), 2),
            "volatility": round(float(vol * 100), 2),
            "ema20": round(float(ema20_value), 2),
            "ema50": round(float(ema50_value), 2),
            "confidence": round(float(min(confidence, 0.95)), 2)
        }


# ==========================================================
# SENTIMENT PRIX
# ==========================================================

class SentimentNarrativeAgent:
    def analyze(self, regime: str, market_context: Dict[str, Any] | None = None) -> Dict[str, Any]:
        market_context = market_context or {}

        base = {
            "Bull": 0.25,
            "Bear": -0.25,
            "Sideways": 0.0,
            "Volatile": -0.05,
            "Unknown": 0.0
        }.get(regime, 0.0)

        momentum = float(market_context.get("momentum", 0.0))
        volume_z = float(market_context.get("volume_z", 0.0))
        rsi_value = float(market_context.get("rsi", 50.0))

        score = base

        if momentum > 0.20:
            score += 0.18
        elif momentum < -0.20:
            score -= 0.18

        if volume_z > 1.2 and momentum > 0:
            score += 0.10
        elif volume_z > 1.2 and momentum < 0:
            score -= 0.10

        if rsi_value > 76:
            score -= 0.18
        elif rsi_value < 24:
            score += 0.18

        score = clamp(score)

        if score >= 0.40:
            narrative = "Flux acheteur dominant"
        elif score <= -0.40:
            narrative = "Pression vendeuse dominante"
        elif score > 0.12:
            narrative = "Biais légèrement haussier"
        elif score < -0.12:
            narrative = "Biais légèrement baissier"
        else:
            narrative = "Marché neutre / indécis"

        return {
            "sentiment_score": round(float(score), 2),
            "narrative": narrative,
            "confidence": round(0.60 + min(abs(score), 0.6) * 0.45, 2)
        }


# ==========================================================
# TECHNIQUE MULTI TIMEFRAME
# ==========================================================

class TechnicalDeepAgent:
    def analyze_one(self, df: pd.DataFrame) -> Dict[str, Any]:
        close = df["close"]

        ema20_value = ema(close, 20).iloc[-1]
        ema50_value = ema(close, 50).iloc[-1]
        ema200_value = ema(close, 200).iloc[-1] if len(close) >= 200 else ema50_value

        rsi_value = rsi(close, 14).iloc[-1]
        macd_value = macd_hist(close).iloc[-1]
        atr_value = atr(df, 14).iloc[-1]

        price = close.iloc[-1]

        if price > ema20_value > ema50_value:
            trend = "Bullish"
            trend_score = 0.32
        elif price < ema20_value < ema50_value:
            trend = "Bearish"
            trend_score = -0.32
        else:
            trend = "Neutral"
            trend_score = 0.0

        rsi_score = 0.0

        if 45 <= rsi_value <= 68:
            rsi_score = 0.12
        elif 32 <= rsi_value <= 55:
            rsi_score = -0.02
        elif rsi_value > 76:
            rsi_score = -0.25
        elif rsi_value < 24:
            rsi_score = 0.20

        macd_score = 0.12 if macd_value > 0 else -0.12

        raw_signal = 0.50 + trend_score + rsi_score + macd_score
        ml_signal = float(np.clip(raw_signal, 0.05, 0.95))

        recent_high = df["high"].tail(30).max()
        recent_low = df["low"].tail(30).min()

        return {
            "price": round(float(price), 2),
            "ema20": round(float(ema20_value), 2),
            "ema50": round(float(ema50_value), 2),
            "ema200": round(float(ema200_value), 2),
            "rsi": round(float(rsi_value), 1),
            "macd_hist": round(float(macd_value), 4),
            "atr": round(float(atr_value), 4),
            "trend": trend,
            "ml_signal": round(float(ml_signal), 3),
            "recent_high": round(float(recent_high), 2),
            "recent_low": round(float(recent_low), 2),
            "confidence": round(0.62 + abs(ml_signal - 0.5) * 0.55, 2)
        }

    def analyze(self, data: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
        m15 = self.analyze_one(data["15m"])
        h1 = self.analyze_one(data["1h"])
        h4 = self.analyze_one(data["4h"])

        trends = [m15["trend"], h1["trend"], h4["trend"]]

        if trends.count("Bullish") >= 2:
            alignment = "Bullish"
            alignment_score = 0.28
        elif trends.count("Bearish") >= 2:
            alignment = "Bearish"
            alignment_score = -0.28
        else:
            alignment = "Mixed"
            alignment_score = 0.0

        combined_signal = (
            m15["ml_signal"] * 0.25 +
            h1["ml_signal"] * 0.35 +
            h4["ml_signal"] * 0.40
        )

        technical_score = clamp((combined_signal - 0.5) * 2 + alignment_score)

        return {
            "ml_signal": round(float(combined_signal), 3),
            "technical_score": round(float(technical_score), 3),
            "rsi": h1["rsi"],
            "trend": alignment,
            "alignment": alignment,
            "alignment_score": alignment_score,
            "confidence": round(0.64 + abs(combined_signal - 0.5) * 0.60, 2),
            "m15": m15,
            "h1": h1,
            "h4": h4
        }


# ==========================================================
# RISK
# ==========================================================

class RiskPortfolioAgent:
    def __init__(self, risk_engine: RiskEngine):
        self.risk_engine = risk_engine

    def validate(
        self,
        equity: float,
        proposed_size: float,
        current_dd: float,
        regime: str,
        price: float,
        atr: float,
        daily_pnl: float = 0.0
    ) -> Dict[str, Any]:

        can_trade, reason = self.risk_engine.check_risk_limits(
            current_equity=equity,
            peak_equity=equity,
            daily_pnl=daily_pnl
        )

        if not can_trade:
            return {
                "approved": False,
                "reason": reason,
                "size": 0.0,
                "risk_pct": 0.0
            }

        if price <= 0 or atr <= 0:
            risk_pct = 0.0
        else:
            risk_pct = round((atr * 1.8) / price * 100, 2)

        return {
            "approved": True,
            "size": proposed_size,
            "risk_pct": risk_pct,
            "reason": "Risk OK - alerte manuelle"
        }


# ==========================================================
# STRATEGIST V3
# ==========================================================

class StrategistCoordinator:
    def timing_score(self) -> float:
        hour_utc = datetime.utcnow().hour

        # Londres / New York : meilleure liquidité pour l'or
        if 7 <= hour_utc <= 20:
            return 0.10

        return -0.05

    def decide(
        self,
        regime: Dict,
        sentiment: Dict,
        technical: Dict,
        risk: Dict,
        price: float,
        macro: Dict | None = None,
        geo: Dict | None = None
    ) -> Dict[str, Any]:

        macro = macro or {"score": 0.0, "narrative": "Macro neutre"}
        geo = geo or {"score": 0.0, "narrative": "Géopolitique neutre"}

        if not risk.get("approved", False):
            return {
                "action": "HOLD",
                "size_pct": 0.0,
                "explanation": f"VETO RISK: {risk.get('reason')}",
                "composite_score": 0.0,
                "global_score": 50,
                "confidence": 0.80
            }

        regime_name = regime.get("regime", "Unknown")

        regime_score = {
            "Bull": 0.15,
            "Bear": -0.15,
            "Sideways": 0.0,
            "Volatile": -0.06,
            "Unknown": 0.0
        }.get(regime_name, 0.0)

        technical_score = float(technical.get("technical_score", 0.0))
        macro_score = float(macro.get("score", 0.0))
        geo_score = float(geo.get("score", 0.0))
        price_sentiment = float(sentiment.get("sentiment_score", 0.0))
        timing = self.timing_score()

        final_score = (
            technical_score * 0.40 +
            macro_score * 0.30 +
            geo_score * 0.20 +
            price_sentiment * 0.07 +
            timing * 0.03 +
            regime_score * 0.10
        )

        final_score = clamp(final_score)
        global_score = round((final_score + 1) * 50, 1)

        h1_rsi = technical.get("h1", {}).get("rsi", 50)
        h4_trend = technical.get("h4", {}).get("trend", "Neutral")
        h1_trend = technical.get("h1", {}).get("trend", "Neutral")
        m15_trend = technical.get("m15", {}).get("trend", "Neutral")

        action = "HOLD"

        long_conditions = (
            final_score >= 0.35 and
            h1_trend == "Bullish" and
            h4_trend in ["Bullish", "Neutral"] and
            h1_rsi < 76
        )

        short_conditions = (
            final_score <= -0.35 and
            h1_trend == "Bearish" and
            h4_trend in ["Bearish", "Neutral"] and
            h1_rsi > 24
        )

        if long_conditions:
            action = "LONG"
        elif short_conditions:
            action = "SHORT"

        confidence = 0.62 + min(abs(final_score), 0.85) * 0.42

        size_pct = 0.0
        if action in ["LONG", "SHORT"]:
            size_pct = min(0.025, max(0.006, abs(final_score) * 0.025))

        explanation = (
            f"Tech={technical_score:+.2f} | "
            f"Macro={macro_score:+.2f} | "
            f"Geo={geo_score:+.2f} | "
            f"Timing={timing:+.2f} | "
            f"H4={h4_trend} | H1={h1_trend} | M15={m15_trend} | "
            f"RSI H1={h1_rsi} | "
            f"Score global={global_score}/100 → {action}"
        )

        return {
            "action": action,
            "size_pct": round(float(size_pct), 4),
            "explanation": explanation,
            "composite_score": round(float(final_score), 3),
            "global_score": global_score,
            "confidence": round(float(min(confidence, 0.94)), 2)
        }
