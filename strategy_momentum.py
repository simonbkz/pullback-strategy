"""Momentum strategy — third signal family in the ensemble.

Signal:
  LONG:  20-bar close-to-close move exceeds MOMENTUM_ATR_MULT × ATR AND
         HTF trend is up. Fires only on the FIRST bar where condition
         becomes true (not while sustained).
  SHORT: mirror.

Different from pullback (which enters counter-move on a dip) and from
breakout (which needs a specific channel break). This one just says
"price has moved strongly with the trend, get in".

Same schema as strategy.generate_signals and strategy_breakout.generate_signals.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

from config import StrategyParams
from strategy import ema, atr, htf_trend, align_htf_to_ltf


MOMENTUM_LOOKBACK = 20         # bars to measure move
MOMENTUM_ATR_MULT = 3.0        # move must be >= this many ATRs


def generate_signals(ltf: pd.DataFrame, htf: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    sig = pd.DataFrame(index=ltf.index)
    sig["side"] = pd.Series([None] * len(ltf), dtype=object)
    sig["entry"] = np.nan
    sig["sl"] = np.nan
    sig["tp"] = np.nan
    sig["atr_at_signal"] = np.nan

    if len(ltf) < max(p.slow_ema, MOMENTUM_LOOKBACK) + 5:
        return sig

    a = atr(ltf, p.atr_period)
    move = ltf["close"] - ltf["close"].shift(MOMENTUM_LOOKBACK)
    threshold = MOMENTUM_ATR_MULT * a

    htf_t = align_htf_to_ltf(htf_trend(htf, p), ltf.index)

    long_mom = (move > threshold) & (htf_t == 1)
    short_mom = (move < -threshold) & (htf_t == -1)

    # Fire only on FRESH condition (edge-triggered, not level-triggered)
    long_new = long_mom & ~long_mom.shift(1).fillna(False).astype(bool)
    short_new = short_mom & ~short_mom.shift(1).fillna(False).astype(bool)

    next_open = ltf["open"].shift(-1)

    # Same ATR SL / RR TP structure as siblings
    sl_long = ltf["close"].values - p.atr_stop_mult * a.values
    risk_long = next_open.values - sl_long
    tp_long = next_open.values + p.rr_target * risk_long

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
