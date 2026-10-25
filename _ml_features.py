"""Feature engineering for ML experiments.

Computes a feature matrix from H1 bars for a given instrument.
Features at each H1 bar reflect ONLY backward-looking information up to that bar.
"""
import pandas as pd
import numpy as np
from pathlib import Path


def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def _atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([(h-l).abs(), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def _rsi(close, n=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0).ewm(alpha=1/n, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def build_features(h1: pd.DataFrame, h4: pd.DataFrame | None = None) -> pd.DataFrame:
    """Return features aligned to h1 index. All backward-looking."""
    df = pd.DataFrame(index=h1.index)
    close = h1["close"]
    atr = _atr(h1, 14)
    atr_norm = atr / atr.rolling(500, min_periods=50).mean()

    # Price-based features
    df["ret_1"] = close.pct_change(1)
    df["ret_5"] = close.pct_change(5)
    df["ret_20"] = close.pct_change(20)
    df["ret_60"] = close.pct_change(60)
    df["log_ret_20"] = np.log(close).diff(20)

    # Trend features
    ema20 = _ema(close, 20)
    ema50 = _ema(close, 50)
    ema200 = _ema(close, 200)
    df["dist_ema20"] = (close - ema20) / atr
    df["dist_ema50"] = (close - ema50) / atr
    df["dist_ema200"] = (close - ema200) / atr
    df["ema_cross_50_200"] = (ema50 - ema200) / atr
    df["ema_cross_20_50"] = (ema20 - ema50) / atr

    # Volatility features
    df["atr_norm"] = atr_norm
    df["range_rel"] = (h1["high"] - h1["low"]) / atr

    # Momentum
    df["rsi"] = _rsi(close, 14)
    df["rsi_prev"] = df["rsi"].shift(5)

    # Position within recent range
    high_20 = h1["high"].rolling(20).max()
    low_20 = h1["low"].rolling(20).min()
    df["pct_range_20"] = (close - low_20) / (high_20 - low_20).replace(0, np.nan)

    # Time-of-day one-hot (compact)
    df["hour_sin"] = np.sin(2 * np.pi * h1.index.hour / 24)
    df["hour_cos"] = np.cos(2 * np.pi * h1.index.hour / 24)
    df["dow"] = h1.index.dayofweek.astype(float)

    # H4 trend (if provided)
    if h4 is not None and len(h4) > 200:
        h4_clean = h4[~h4.index.duplicated(keep='last')]
        h4_ema50 = _ema(h4_clean["close"], 50)
        h4_ema200 = _ema(h4_clean["close"], 200)
        h4_trend = pd.Series(0, index=h4_clean.index, dtype=float)
        h4_trend[(h4_clean["close"] > h4_ema200) & (h4_ema50 > h4_ema200)] = 1.0
        h4_trend[(h4_clean["close"] < h4_ema200) & (h4_ema50 < h4_ema200)] = -1.0
        # Reindex to h1 (forward fill from H4 to each H1 bar)
        h1_clean_idx = h1.index[~h1.index.duplicated(keep='last')]
        combined = h1_clean_idx.union(h4_trend.index)
        h4_trend_h1 = h4_trend.reindex(combined).ffill().reindex(h1.index)
        df["h4_trend"] = h4_trend_h1
    else:
        df["h4_trend"] = 0.0

    return df


FEATURE_COLS = [
    "ret_1", "ret_5", "ret_20", "ret_60", "log_ret_20",
    "dist_ema20", "dist_ema50", "dist_ema200", "ema_cross_50_200", "ema_cross_20_50",
    "atr_norm", "range_rel",
    "rsi", "rsi_prev",
    "pct_range_20",
    "hour_sin", "hour_cos", "dow",
    "h4_trend",
]
