"""Instrument universe, timeframes, strategy parameters.

Single source of truth: each tradeable instrument is an `Instrument` with
its yfinance ticker, asset class, pip size (smallest meaningful price
increment for spread accounting), and typical broker spread in pips.

Spreads are HFM Premium-account ballparks. They widen during news/rollover.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    ticker: str          # yfinance ticker symbol
    name: str            # display name (also used as cache filename)
    asset_class: str     # fx_major / fx_cross / index / commodity / crypto
    pip_size: float      # price units per pip (used for spread + pnl scaling)
    spread_pips: float   # typical broker spread in pips


# yfinance ticker conventions:
#   FX spot     "EURUSD=X"
#   Index       "^GSPC", "^NDX", ...
#   Futures     "GC=F" (continuous front-month)
#   Crypto      "BTC-USD"
INSTRUMENTS: list[Instrument] = [
    # ---------------- FX majors ----------------
    Instrument("EURUSD=X", "EURUSD", "fx_major", 0.0001, 1.2),
    Instrument("GBPUSD=X", "GBPUSD", "fx_major", 0.0001, 1.5),
    Instrument("USDJPY=X", "USDJPY", "fx_major", 0.01,   1.3),
    Instrument("USDCHF=X", "USDCHF", "fx_major", 0.0001, 1.7),
    Instrument("AUDUSD=X", "AUDUSD", "fx_major", 0.0001, 1.5),
    Instrument("NZDUSD=X", "NZDUSD", "fx_major", 0.0001, 1.9),
    Instrument("USDCAD=X", "USDCAD", "fx_major", 0.0001, 1.7),
    # ---------------- FX crosses ----------------
    Instrument("EURGBP=X", "EURGBP", "fx_cross", 0.0001, 1.6),
    Instrument("EURJPY=X", "EURJPY", "fx_cross", 0.01,   1.7),
    Instrument("EURCHF=X", "EURCHF", "fx_cross", 0.0001, 2.0),
    Instrument("EURAUD=X", "EURAUD", "fx_cross", 0.0001, 2.5),
    Instrument("EURNZD=X", "EURNZD", "fx_cross", 0.0001, 3.0),
    Instrument("EURCAD=X", "EURCAD", "fx_cross", 0.0001, 2.5),
    Instrument("GBPJPY=X", "GBPJPY", "fx_cross", 0.01,   2.5),
    Instrument("GBPCHF=X", "GBPCHF", "fx_cross", 0.0001, 3.0),
    Instrument("GBPAUD=X", "GBPAUD", "fx_cross", 0.0001, 3.5),
    Instrument("GBPNZD=X", "GBPNZD", "fx_cross", 0.0001, 4.5),
    Instrument("GBPCAD=X", "GBPCAD", "fx_cross", 0.0001, 3.5),
    Instrument("AUDJPY=X", "AUDJPY", "fx_cross", 0.01,   2.0),
    Instrument("AUDCHF=X", "AUDCHF", "fx_cross", 0.0001, 2.5),
    Instrument("AUDNZD=X", "AUDNZD", "fx_cross", 0.0001, 3.0),
    Instrument("AUDCAD=X", "AUDCAD", "fx_cross", 0.0001, 2.5),
    Instrument("NZDJPY=X", "NZDJPY", "fx_cross", 0.01,   2.5),
    Instrument("NZDCHF=X", "NZDCHF", "fx_cross", 0.0001, 3.0),
    Instrument("NZDCAD=X", "NZDCAD", "fx_cross", 0.0001, 3.0),
    Instrument("CADJPY=X", "CADJPY", "fx_cross", 0.01,   2.5),
    Instrument("CADCHF=X", "CADCHF", "fx_cross", 0.0001, 2.5),
    Instrument("CHFJPY=X", "CHFJPY", "fx_cross", 0.01,   2.5),
    # ---------------- Indices (HFM CFDs) ----------------
    # pip_size = 0.1 of an index point.
    Instrument("^GSPC",   "US500",  "index", 0.1,  5.0),    # S&P 500
    Instrument("^NDX",    "US100",  "index", 0.1, 15.0),    # NASDAQ 100
    Instrument("^DJI",    "US30",   "index", 0.1, 30.0),    # Dow Jones
    Instrument("^RUT",    "US2000", "index", 0.1, 10.0),    # Russell 2000
    Instrument("^GDAXI",  "DE40",   "index", 0.1, 10.0),    # DAX
    Instrument("^FTSE",   "UK100",  "index", 0.1, 10.0),    # FTSE 100
    Instrument("^N225",   "JP225",  "index", 0.1,100.0),    # Nikkei (yen-denominated, larger absolute moves)
    Instrument("^HSI",    "HK50",   "index", 0.1, 50.0),    # Hang Seng
    Instrument("^STOXX50E","EU50",  "index", 0.1, 10.0),    # Euro Stoxx 50
    # ---------------- Commodities ----------------
    Instrument("GC=F", "XAUUSD",   "commodity", 0.10, 3.0),    # Gold (per troy oz)
    Instrument("SI=F", "XAGUSD",   "commodity", 0.01, 2.5),    # Silver
    Instrument("CL=F", "USOIL",    "commodity", 0.01, 3.0),    # WTI crude
    Instrument("BZ=F", "UKOIL",    "commodity", 0.01, 4.0),    # Brent crude
    Instrument("NG=F", "NATGAS",   "commodity", 0.001, 5.0),   # Natural gas
    Instrument("HG=F", "COPPER",   "commodity", 0.0001, 5.0),  # Copper
    Instrument("PL=F", "XPTUSD",   "commodity", 0.10, 5.0),    # Platinum
    # ---------------- Bonds (cross-asset diversifiers — added 2026-05-14) ----------------
    # HFM symbols: US10YR.F (10Y Treasury), EUBUND.F (German Bund), UKGILT.F (UK Gilt)
    # Pulled via fetch_mt5_bars.py (MT5 history, not yfinance). 2-year H1 history available.
    Instrument("US10YR.F", "US10Y",  "bond", 0.01, 2.0),
    Instrument("EUBUND.F", "EUBUND", "bond", 0.01, 2.0),
    Instrument("UKGILT.F", "UKGILT", "bond", 0.01, 2.0),
    # ---------------- Additional indices (cross-region diversifiers) ----------------
    Instrument("US30.F",   "US30",   "index", 0.1, 30.0),
    Instrument("UK100",    "UK100",  "index", 0.1, 10.0),
    Instrument("AUS200",   "AUS200", "index", 0.1, 10.0),
    Instrument("FRA40",    "FRA40",  "index", 0.1, 10.0),
    Instrument("EU50.F",   "EU50",   "index", 0.1, 10.0),
    # ---------------- Additional commodities ----------------
    Instrument("Copper",   "COPPER", "commodity", 0.0001, 5.0),
    Instrument("Platinum", "PLATIN", "commodity", 0.10, 5.0),
    # ---------------- Crypto ----------------
    Instrument("BTC-USD", "BTCUSD", "crypto", 1.0,  50.0),
    Instrument("ETH-USD", "ETHUSD", "crypto", 0.1,  30.0),
    Instrument("SOL-USD", "SOLUSD", "crypto", 0.01, 20.0),
    Instrument("BNB-USD", "BNBUSD", "crypto", 0.1,  20.0),
    Instrument("XRP-USD", "XRPUSD", "crypto", 0.0001, 10.0),
]


def by_name(name: str) -> Instrument | None:
    for ins in INSTRUMENTS:
        if ins.name == name:
            return ins
    return None


# yfinance hourly is capped at ~730 days of history.
INTERVAL_ENTRY = "1h"
INTERVAL_HIGHER = "4h"
PERIOD = "730d"  # ~2 years

DATA_DIR = "data"
REPORT_DIR = "reports"


@dataclass(frozen=True)
class StrategyParams:
    fast_ema: int = 50
    slow_ema: int = 200
    pullback_ema: int = 20
    swing_lookback: int = 20
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    rr_target: float = 2.5
    risk_pct: float = 1.0
    pin_body_to_range: float = 0.35
    pin_wick_to_body: float = 2.0

    # --- Confirmation correctness (defaults preserve original behavior) ---
    pin_min_body_atr: float = 0.0          # >0 enforces min body as fraction of ATR
    strict_engulf: bool = False            # True = full body engulfment of prior body

    # --- Trade management ---
    use_breakeven: bool = False
    breakeven_at_r: float = 1.0
    use_trailing: bool = False
    trail_atr_mult: float = 2.0

    # --- Session filter (UTC hours, inclusive bounds) ---
    session_filter: bool = False
    session_start_utc: int = 7             # London open
    session_end_utc: int = 16              # NY pm

    # --- Volatility regime gate (ATR normalised by rolling mean of ATR) ---
    vol_regime_filter: bool = False
    atr_norm_window: int = 500
    atr_norm_min: float = 0.60             # skip if ATR < 60% of rolling mean
    atr_norm_max: float = 2.00             # skip if ATR > 200% of rolling mean

    # --- Execution safety ---
    max_bars_in_trade: int = 200


DEFAULT_PARAMS = StrategyParams()
