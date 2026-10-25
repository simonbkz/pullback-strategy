"""Regime classification.

Per-bar label of {trending | neutral | chop} from ADX(14) on the entry-TF
series. Plus a (instrument, ts) -> regime lookup that handles tz alignment
between Trade timestamps and cached parquet indices.

For the trend-pullback strategy:
  - "trending" (high ADX) is the regime where the strategy has its real edge
  - "chop"     (low ADX)  is where it bleeds (false breakouts back to mean)
  - "neutral"  is in between

Risk scaling will up-weight trending and down-weight chop.
"""
from __future__ import annotations
import pandas as pd

from features import _adx


def compute_regime_series(
    ltf: pd.DataFrame,
    adx_period: int = 14,
    trend_threshold: float = 25.0,
    chop_threshold: float = 20.0,
) -> pd.Series:
    """Return regime label per bar."""
    adx = _adx(ltf, adx_period)
    regime = pd.Series("neutral", index=ltf.index, dtype=object)
    regime[adx >= trend_threshold] = "trending"
    regime[adx < chop_threshold] = "chop"
    return regime


def build_regime_lookup(ltf_by_name: dict[str, pd.DataFrame]) -> callable:
    """Build a (instrument_name, timestamp) -> regime function from cached series."""
    by_name = {name: compute_regime_series(ltf) for name, ltf in ltf_by_name.items()}

    def lookup(name: str, ts) -> str:
        s = by_name.get(name)
        if s is None or s.empty:
            return "neutral"
        t = pd.Timestamp(ts)
        idx = s.index
        # Align tz
        if idx.tz is None:
            t = t.tz_localize(None) if t.tzinfo is None else t.tz_convert("UTC").tz_localize(None)
        else:
            t = t.tz_localize(idx.tz) if t.tzinfo is None else t.tz_convert(idx.tz)
        try:
            pos = idx.get_indexer([t], method="nearest")[0]
            if pos < 0:
                return "neutral"
            return s.iloc[pos]
        except Exception:
            return "neutral"

    return lookup


def regime_distribution(ltf_by_name: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Per-instrument fraction of bars in each regime."""
    rows = []
    for name, ltf in ltf_by_name.items():
        s = compute_regime_series(ltf)
        vc = s.value_counts(normalize=True)
        rows.append({
            "instrument": name,
            "trending_pct": float(vc.get("trending", 0.0) * 100),
            "neutral_pct":  float(vc.get("neutral",  0.0) * 100),
            "chop_pct":     float(vc.get("chop",     0.0) * 100),
            "n_bars": len(s),
        })
    return pd.DataFrame(rows)
