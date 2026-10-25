"""Pullback trend strategy — Python mirror of PullbackTrendEA.mq5.

Higher-timeframe trend filter (close vs 200 EMA + 50 vs 200 EMA cross),
H1 retracement to 20 EMA, pin-bar / engulfing confirmation, ATR-or-swing
stop, fixed R-multiple target. Generates signals only — execution +
spread accounting lives in backtest.py.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

from config import StrategyParams


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def htf_trend(htf: pd.DataFrame, p: StrategyParams) -> pd.Series:
    """+1 / -1 / 0 trend on each higher-TF bar (set on bar close)."""
    fast = ema(htf["close"], p.fast_ema)
    slow = ema(htf["close"], p.slow_ema)
    bull = (htf["close"] > slow) & (fast > slow)
    bear = (htf["close"] < slow) & (fast < slow)
    out = pd.Series(0, index=htf.index, dtype=int)
    out[bull] = 1
    out[bear] = -1
    return out


def align_htf_to_ltf(htf_signal: pd.Series, ltf_index: pd.DatetimeIndex) -> pd.Series:
    """For each LTF bar, the trend value of the most recent *fully closed* HTF bar.

    yfinance HTF timestamps are bar-open. Publish each HTF signal at the next
    HTF index (i.e. when that bar actually closes), then ffill onto the LTF.
    """
    s = htf_signal.copy()
    if len(s) >= 2:
        # BUG FIX 2026-07-05: was s.index[1] - s.index[0] which returns 76h
        # if the first two bars span a weekend gap (Fri 20 -> Mon 00). Hardcode
        # 4h for H4, matching data.py:to_h4 resample origin.
        bar = pd.Timedelta("4h")
        s.index = s.index + bar
    aligned = s.reindex(ltf_index, method="ffill").fillna(0).astype(int)
    return aligned


def is_bull_pin_or_engulf(df: pd.DataFrame, p: StrategyParams, a: pd.Series | None = None) -> pd.Series:
    o, c, h, l = df["open"], df["close"], df["high"], df["low"]
    o2, c2 = o.shift(1), c.shift(1)
    rng = (h - l).replace(0, np.nan)
    body = (c - o).abs()
    lower_wick = np.minimum(o, c) - l

    body_floor = 0.0
    if a is not None and p.pin_min_body_atr > 0:
        body_floor = p.pin_min_body_atr * a

    pin = (
        (c > o)
        & (lower_wick > p.pin_wick_to_body * body)
        & (body / rng < p.pin_body_to_range)
        & (body > body_floor)
    )

    if p.strict_engulf:
        body_top2 = np.maximum(o2, c2)
        body_bot2 = np.minimum(o2, c2)
        engulf = (c > o) & (c2 < o2) & (c >= body_top2) & (o <= body_bot2)
    else:
        engulf = (c > o) & (c2 < o2) & (c > o2) & (o < c2)
    return (pin | engulf).fillna(False)


def is_bear_pin_or_engulf(df: pd.DataFrame, p: StrategyParams, a: pd.Series | None = None) -> pd.Series:
    o, c, h, l = df["open"], df["close"], df["high"], df["low"]
    o2, c2 = o.shift(1), c.shift(1)
    rng = (h - l).replace(0, np.nan)
    body = (c - o).abs()
    upper_wick = h - np.maximum(o, c)

    body_floor = 0.0
    if a is not None and p.pin_min_body_atr > 0:
        body_floor = p.pin_min_body_atr * a

    pin = (
        (c < o)
        & (upper_wick > p.pin_wick_to_body * body)
        & (body / rng < p.pin_body_to_range)
        & (body > body_floor)
    )

    if p.strict_engulf:
        body_top2 = np.maximum(o2, c2)
        body_bot2 = np.minimum(o2, c2)
        engulf = (c < o) & (c2 > o2) & (c <= body_bot2) & (o >= body_top2)
    else:
        engulf = (c < o) & (c2 > o2) & (c < o2) & (o > c2)
    return (pin | engulf).fillna(False)


def generate_signals(ltf: pd.DataFrame, htf: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    """Return a DataFrame indexed like ltf with columns:
    side ('long'/'short'/None), entry, sl, tp, atr_at_signal.
    Signal is on the close of bar i; entry happens at bar i+1 open in the
    backtester (no look-ahead).
    """
    sig = pd.DataFrame(index=ltf.index)
    sig["side"] = pd.Series([None] * len(ltf), dtype=object)
    sig["entry"] = np.nan
    sig["sl"] = np.nan
    sig["tp"] = np.nan
    sig["atr_at_signal"] = np.nan

    if len(ltf) < max(p.slow_ema, p.swing_lookback) + 5:
        return sig

    pullback_ema = ema(ltf["close"], p.pullback_ema)
    a = atr(ltf, p.atr_period)
    swing_low = ltf["low"].rolling(p.swing_lookback).min().shift(1)
    swing_high = ltf["high"].rolling(p.swing_lookback).max().shift(1)

    bull_conf = is_bull_pin_or_engulf(ltf, p, a)
    bear_conf = is_bear_pin_or_engulf(ltf, p, a)

    pulled_back_long = ltf["low"] <= pullback_ema
    pulled_back_short = ltf["high"] >= pullback_ema

    htf_t = align_htf_to_ltf(htf_trend(htf, p), ltf.index)

    long_sig = (htf_t == 1) & pulled_back_long & bull_conf
    short_sig = (htf_t == -1) & pulled_back_short & bear_conf

    # Entry at next bar open. Stops and targets computed from signal-bar values.
    next_open = ltf["open"].shift(-1)

    # LONG
    sl_long = np.minimum(swing_low.values, ltf["close"].values - p.atr_stop_mult * a.values)
    risk_long = next_open.values - sl_long
    tp_long = next_open.values + p.rr_target * risk_long

    # SHORT
    sl_short = np.maximum(swing_high.values, ltf["close"].values + p.atr_stop_mult * a.values)
    risk_short = sl_short - next_open.values
    tp_short = next_open.values - p.rr_target * risk_short

    long_idx = long_sig & (risk_long > 0) & np.isfinite(next_open)
    short_idx = short_sig & (risk_short > 0) & np.isfinite(next_open)

    sig.loc[long_idx, "side"] = "long"
    sig.loc[long_idx, "entry"] = next_open[long_idx]
    sig.loc[long_idx, "sl"] = sl_long[long_idx.values]
    sig.loc[long_idx, "tp"] = tp_long[long_idx.values]
    sig.loc[long_idx, "atr_at_signal"] = a[long_idx]

    sig.loc[short_idx, "side"] = "short"
    sig.loc[short_idx, "entry"] = next_open[short_idx]
    sig.loc[short_idx, "sl"] = sl_short[short_idx.values]
    sig.loc[short_idx, "tp"] = tp_short[short_idx.values]
    sig.loc[short_idx, "atr_at_signal"] = a[short_idx]

    return sig
