from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # === Source de données ===
    # TWELVEDATA = XAU/USD via API Twelve Data
    # YAHOO = fallback gratuit mais limité
    # MT5 = vrai XAUUSD via MetaTrader 5 sur Windows/VPS
    # BINANCE = fallback PAXG/USDT
    DATA_SOURCE: str = "TWELVEDATA"

    # === Symboles ===
    SYMBOL: str = "XAUUSD"
    YAHOO_SYMBOL: str = "GC=F"
    MT5_SYMBOL: str = "XAUUSD"
    BINANCE_SYMBOL: str = "PAXG/USDT"

    # === Twelve Data ===
    TWELVEDATA_SYMBOL: str = "XAU/USD"
    TWELVEDATA_API_KEY: str = ""

    # === Exchange legacy / compatibilité ===
    EXCHANGE: str = "binance"
    MARKET_TYPE: str = "spot"
    API_KEY: str = ""
    API_SECRET: str = ""
    API_PASSWORD: Optional[str] = None
    TESTNET: bool = False

    # === Telegram ===
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[int] = None

    # === Mode ===
    MODE: str = "live"

    # === Alertes uniquement ===
    LIVE_TRADE_NOTIONAL_USDT: float = 0.0
    ALLOW_SHORTS: bool = False

    # === Risk Engine ===
    MAX_RISK_PER_TRADE: float = 0.012
    MAX_DAILY_LOSS: float = 0.025
    MAX_TOTAL_DRAWDOWN: float = 0.13

    # === Macro / News ===
    GEO_LOOKBACK_HOURS: int = 6
    GEO_MAX_ARTICLES: int = 25

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
