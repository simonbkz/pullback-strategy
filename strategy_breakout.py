"""Donchian breakout strategy — alternative signal family to pullback-trend.

Signal:
  LONG:  ltf close breaks above the N-bar high (excluding current bar) AND
         HTF trend is up (EMA-fast > EMA-slow AND close > EMA-slow).
  SHORT: mirror image.

Filters and management use the same infrastructure as `strategy.generate_signals`:
  - Same HTF trend gate (`htf_trend`, `align_htf_to_ltf`) for consistency.
  - Same ATR-based SL (atr_stop_mult) and RR-based TP (rr_target).
  - Same entry convention: signal on close of bar i, entry at bar i+1 open.

Different edge: pullback-trend profits from mean-reversion inside a trend;
breakout profits from continuation after price escapes a range. In market
regimes where price consolidates then explodes, breakout should outperform
pullback (which never triggers because price never pulls back to EMA20).
"""
from __future__ import annotations
import numpy as np
import pandas as pd

from config import StrategyParams
from strategy import ema, atr, htf_trend, align_htf_to_ltf


DONCHIAN_LOOKBACK = 20


def generate_signals(ltf: pd.DataFrame, htf: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    """Same schema as strategy.generate_signals."""
    sig = pd.DataFrame(index=ltf.index)
    sig["side"] = pd.Series([None] * len(ltf), dtype=object)
    sig["entry"] = np.nan
    sig["sl"] = np.nan
    sig["tp"] = np.nan
    sig["atr_at_signal"] = np.nan

    if len(ltf) < max(p.slow_ema, DONCHIAN_LOOKBACK) + 5:
        return sig

    a = atr(ltf, p.atr_period)
    dc_high = ltf["high"].rolling(DONCHIAN_LOOKBACK).max().shift(1)
    dc_low = ltf["low"].rolling(DONCHIAN_LOOKBACK).min().shift(1)

    htf_t = align_htf_to_ltf(htf_trend(htf, p), ltf.index)

    # Breakout condition: current bar's close crosses the channel edge
    # AND HTF trend agrees.
    long_break = (ltf["close"] > dc_high) & (htf_t == 1)
    short_break = (ltf["close"] < dc_low) & (htf_t == -1)

    # Only signal ON THE BAR OF THE BREAK (not while we remain outside the
    # channel). This is done via detecting the transition: previous bar was
    # NOT beyond the channel.
    long_new = long_break & ~long_break.shift(1).fillna(False).astype(bool)
    short_new = short_break & ~short_break.shift(1).fillna(False).astype(bool)

    next_open = ltf["open"].shift(-1)

    # LONG
    sl_long = ltf["close"].values - p.atr_stop_mult * a.values
    risk_long = next_open.values - sl_long
    tp_long = next_open.values + p.rr_target * risk_long

    # SHORT
    sl_short = ltf["close"].values + p.atr_stop_mult * a.values
    risk_short = sl_short - next_open.values
    tp_short = next_open.values - p.rr_target * risk_short

    long_idx = long_new & (risk_long > 0) & np.isfinite(next_open)
    short_idx = short_new & (risk_short > 0) & np.isfinite(next_open)

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
