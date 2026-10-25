"""Feature engineering for the ML filter.

For each signal bar (the bar on which a long/short would have been issued),
compute a row of features describing the market state at signal-close. We
deliberately use only past-bar information so the filter can be applied in
real time. Target = 1 if the trade ended profitable after spread, else 0.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

from strategy import ema, atr


def _adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    up_move   = h.diff()
    down_move = -l.diff()
    plus_dm   = ((up_move > down_move) & (up_move > 0)).astype(float) * up_move.clip(lower=0)
    minus_dm  = ((down_move > up_move) & (down_move > 0)).astype(float) * down_move.clip(lower=0)
    a = atr(df, n).replace(0, np.nan)
    plus_di  = 100 * plus_dm.ewm(alpha=1/n, adjust=False).mean()  / a
    minus_di = 100 * minus_dm.ewm(alpha=1/n, adjust=False).mean() / a
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1/n, adjust=False).mean()


def _rsi(s: pd.Series, n: int = 14) -> pd.Series:
    diff = s.diff()
    up = diff.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-diff.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def signal_features(ltf: pd.DataFrame, htf: pd.DataFrame) -> pd.DataFrame:
    """Compute features for every LTF bar (caller filters to signal bars).

    All features use shift(1) implicitly via the indicator definitions or
    explicit ranking, so a row at index i depends only on bars 0..i (no leak).
    """
    f = pd.DataFrame(index=ltf.index)
    a14 = atr(ltf, 14)
    a50 = atr(ltf, 50)
    f["adx14"]  = _adx(ltf, 14)
    f["rsi14"]  = _rsi(ltf["close"], 14)
    f["atr_ratio"] = a14 / a50.replace(0, np.nan)         # vol regime
    f["dist_ema20"] = (ltf["close"] - ema(ltf["close"], 20)) / a14
    f["dist_ema200"] = (ltf["close"] - ema(ltf["close"], 200)) / a14
    f["mom5"]   = (ltf["close"] - ltf["close"].shift(5)) / a14
    f["mom20"]  = (ltf["close"] - ltf["close"].shift(20)) / a14
    f["range_pct"] = (ltf["high"] - ltf["low"]) / ltf["close"].replace(0, np.nan)

    # Time-of-day (cyclical)
    hours = ltf.index.hour.values + ltf.index.minute.values / 60.0
    f["hour_sin"] = np.sin(2 * np.pi * hours / 24)
    f["hour_cos"] = np.cos(2 * np.pi * hours / 24)
    f["dow"] = ltf.index.dayofweek.values

    # HTF context: align last fully-closed HTF bar's RSI/ADX onto LTF index
    htf_rsi = _rsi(htf["close"], 14)
    htf_adx = _adx(htf, 14)
    htf_rsi.index = htf_rsi.index + pd.Timedelta("1ns")
    htf_adx.index = htf_adx.index + pd.Timedelta("1ns")
    f["htf_rsi"] = htf_rsi.reindex(ltf.index, method="ffill")
    f["htf_adx"] = htf_adx.reindex(ltf.index, method="ffill")

    return f
