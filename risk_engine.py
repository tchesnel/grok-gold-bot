"""
GROK TRADER WAR MACHINE - Risk Engine Production
Version robuste avec veto absolu
"""
import numpy as np
from typing import Dict, Tuple

class RiskEngine:
    def __init__(self, settings):
        self.max_risk_per_trade = settings.MAX_RISK_PER_TRADE
        self.max_daily_loss = settings.MAX_DAILY_LOSS
        self.max_total_drawdown = settings.MAX_TOTAL_DRAWDOWN
        self.daily_pnl = 0.0
        self.peak_equity = 0.0

    def calculate_position_size(self, equity: float, entry: float, stop: float, atr: float) -> float:
        if stop <= 0 or entry <= 0:
            return 0.0
        risk_amount = equity * self.max_risk_per_trade
        risk_per_unit = abs(entry - stop)
        if risk_per_unit < 0.0001:
            risk_per_unit = atr * 1.8
        size = risk_amount / risk_per_unit
        return max(0.0, size)

    def check_risk_limits(self, current_equity: float, peak_equity: float, daily_pnl: float) -> Tuple[bool, str]:
        self.peak_equity = max(self.peak_equity, peak_equity)
        current_dd = (self.peak_equity - current_equity) / self.peak_equity if self.peak_equity > 0 else 0

        if current_dd > self.max_total_drawdown:
            return False, f"Drawdown total {current_dd*100:.1f}% > limite {self.max_total_drawdown*100}%"

        if abs(daily_pnl) / self.peak_equity > self.max_daily_loss:
            return False, f"Perte journalière trop élevée"

        return True, "Risk OK"

    def get_safe_size(self, proposed_size: float, current_dd: float, regime: str) -> float:
        dd_factor = max(0.25, 1 - (current_dd / self.max_total_drawdown) * 1.8)
        regime_factor = {"Bull": 1.0, "Sideways": 0.65, "Bear": 0.4, "Volatile": 0.5}.get(regime, 0.7)
        return proposed_size * dd_factor * regime_factor
