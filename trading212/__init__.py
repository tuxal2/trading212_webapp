"""Unofficial Python API for the Trading212 web app (app.trading212.com)."""

from .client import Trading212Client
from .config import Config
from .exceptions import (
    ApiError,
    AuthenticationError,
    ConfigError,
    InvalidSession,
    NetworkError,
    Trading212Error,
    TwoFactorRequired,
)
from .history import (
    DetailRow,
    HistoryDetails,
    HistoryEntry,
    LossPot,
    Position,
    RealizedGain,
    SummaryValue,
    TaxOverview,
    TaxTransaction,
)
from .models import (
    Account,
    Budget,
    Cash,
    Instrument,
    InstrumentSettings,
    LoginResult,
    Order,
    OrderCosts,
    Wallet,
)

__all__ = [
    "Trading212Client", "Config",
    "Account", "Budget", "Cash", "Instrument", "InstrumentSettings", "LoginResult",
    "Order", "OrderCosts", "Wallet",
    "HistoryEntry", "HistoryDetails", "DetailRow", "SummaryValue", "TaxOverview", "LossPot",
    "TaxTransaction", "RealizedGain", "Position",
    "Trading212Error", "ApiError", "AuthenticationError", "ConfigError",
    "InvalidSession", "NetworkError", "TwoFactorRequired",
]
__version__ = "0.1.0"
