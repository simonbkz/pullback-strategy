"""Novelty-based regime scoring.

Continuous Mahalanobis distance from per-instrument training feature
centroid. Complements the categorical 3-bucket regime classifier:
the bucket says 'trend / neutral / chop'; novelty says 'how similar is
this bar to anything we've seen before'.

Why both? A 'trending' bucket label can be misleading if the trend is
expressed in features (vol, distance-from-EMA, recent returns) the model
has never trained on — e.g. a flash-crash trend. Novelty catches that
even when ADX still says 'trending'.

Pipeline per instrument:
  1. Build features (build_features from regime_predictor)
  2. Fit Mahalanobis baseline (mean + pseudo-inv covariance) on first
     train_frac of bars
  3. Score every bar; turn into a risk multiplier via thresholds
     calibrated as percentiles of the training score distribution
"""
from __future__ import annotations
import json
import os
from typing import Any
import numpy as np
import pandas as pd

from regime_predictor import build_features


# Drop trivially-periodic features so they don't dominate the distance metric.
_NOVELTY_FEATURES = [
    "adx14", "rsi14", "atr_ratio",
    "dist_ema20", "dist_ema200",
    "ret5", "ret20", "vol5", "vol20",
]


def fit_baseline(train_features: pd.DataFrame, columns=None) -> dict:
    cols = columns or [c for c in _NOVELTY_FEATURES if c in train_features.columns]
    f = train_features[cols].dropna()
    if len(f) < 50:
        raise ValueError(f"need >=50 train rows, got {len(f)}")
    X = f.values
    mean = X.mean(axis=0)
    cov = np.cov(X, rowvar=False)
    inv_cov = np.linalg.pinv(cov + 1e-6 * np.eye(cov.shape[0]))
    diff = X - mean
    train_scores = np.sqrt(np.einsum("ij,jk,ik->i", diff, inv_cov, diff))
    return {
        "columns": cols, "mean": mean, "inv_cov": inv_cov,
        "p30": float(np.percentile(train_scores, 30)),
        "p70": float(np.percentile(train_scores, 70)),
        "p95": float(np.percentile(train_scores, 95)),
    }


def score_bars(features: pd.DataFrame, baseline: dict) -> pd.Series:
    cols = baseline["columns"]
    f = features[cols].ffill().bfill()
    X = f.values
    diff = X - baseline["mean"]
    s = np.sqrt(np.einsum("ij,jk,ik->i", diff, baseline["inv_cov"], diff))
    return pd.Series(s, index=features.index, name="novelty")


def downscale_only_multiplier(score, p70, p95, floor=0.3) -> float:
    """Familiar (score<p70) -> 1.0; novel (score>p95) -> floor; ramp between."""
    if score < p70:
        return 1.0
    if score > p95:
        return floor
    frac = (score - p70) / max(p95 - p70, 1e-9)
    return 1.0 - frac * (1.0 - floor)


def upscale_familiar_multiplier(score, p30, p70, p95, ceiling=1.5, floor=0.3) -> float:
    """Very familiar (score<p30) -> ceiling; familiar (p30..p70) -> 1.0;
    novel (p70..p95) -> 1.0 down to floor; very novel (>p95) -> floor."""
    if score < p30:
        return ceiling
    if score < p70:
        return 1.0
    if score > p95:
        return floor
    frac = (score - p70) / max(p95 - p70, 1e-9)
    return 1.0 - frac * (1.0 - floor)


def build_multiplier_series(
    ltf_by_name: dict[str, pd.DataFrame],
    train_frac: float = 0.7,
    mode: str = "downscale_only",
    ceiling: float = 1.5,
    floor: float = 0.3,
) -> dict[str, pd.Series]:
    """Per-instrument: fit baseline ONCE on train, score all bars, map to risk multiplier."""
    out: dict[str, pd.Series] = {}
    for name, ltf in ltf_by_name.items():
        if ltf is None or ltf.empty:
            continue
        try:
            feats = build_features(ltf)
            cut = int(len(feats) * train_frac)
            baseline = fit_baseline(feats.iloc[:cut])
        except Exception as e:
            print(f"  [novelty] {name}: skip ({e})")
            continue
        scores = score_bars(feats, baseline)
        if mode == "upscale_familiar":
            mults = scores.apply(lambda s: upscale_familiar_multiplier(
                s, baseline["p30"], baseline["p70"], baseline["p95"], ceiling, floor))
        else:
            mults = scores.apply(lambda s: downscale_only_multiplier(
                s, baseline["p70"], baseline["p95"], floor))
        out[name] = mults
    return out


def walk_forward_multipliers(
    ltf_by_name: dict[str, pd.DataFrame],
    train_window: int = 1500,
    refit_every: int = 200,
    mode: str = "upscale_familiar",
    ceiling: float = 1.5,
    floor: float = 0.3,
) -> dict[str, pd.Series]:
    """Per-instrument: rolling refit of the Mahalanobis baseline.

    At each refit position i (every refit_every bars after the warmup
    train_window), fit baseline on feats[i-train_window:i], then score
    bars [i, i+refit_every) with that baseline. Bars before warmup get
    multiplier = 1.0 (no scaling).
    """
    out: dict[str, pd.Series] = {}
    for name, ltf in ltf_by_name.items():
        if ltf is None or ltf.empty:
            continue
        feats = build_features(ltf)
        n = len(feats)
        mults = pd.Series(1.0, index=feats.index, dtype=float)
        if n < train_window + 1:
            out[name] = mults
            continue
        i = train_window
        last_baseline = None
        while i < n:
            train_slice = feats.iloc[i - train_window:i].dropna()
            try:
                baseline = fit_baseline(train_slice)
                last_baseline = baseline
            except Exception:
                if last_baseline is None:
                    i += refit_every
                    continue
                baseline = last_baseline
            end = min(i + refit_every, n)
            score_slice = feats.iloc[i:end]
            scores = score_bars(score_slice, baseline)
            if mode == "upscale_familiar":
                m_vals = scores.apply(lambda s, b=baseline: upscale_familiar_multiplier(
                    s, b["p30"], b["p70"], b["p95"], ceiling, floor))
            else:
                m_vals = scores.apply(lambda s, b=baseline: downscale_only_multiplier(
                    s, b["p70"], b["p95"], floor))
            mults.iloc[i:end] = m_vals.values
            i += refit_every
        out[name] = mults
    return out


def save_baselines_to_disk(baselines: dict[str, dict], path: str) -> None:
    """Persist per-instrument Mahalanobis baselines to JSON for live consumption.

    Numpy arrays are converted to nested lists. The schema includes columns,
    mean, inv_cov, and percentile cutoffs (p30/p70/p95) so the live trader
    can compute multipliers from a single bar's features without refitting.
    """
    out: dict[str, dict[str, Any]] = {}
    for ins, b in baselines.items():
        mean = b["mean"]
        inv_cov = b["inv_cov"]
        out[ins] = {
            "columns": list(b["columns"]),
            "mean": mean.tolist() if hasattr(mean, "tolist") else list(mean),
            "inv_cov": inv_cov.tolist() if hasattr(inv_cov, "tolist") else inv_cov,
            "p30": float(b["p30"]),
            "p70": float(b["p70"]),
            "p95": float(b["p95"]),
        }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(out, f, indent=2)


def load_baselines_from_disk(path: str) -> dict[str, dict]:
    """Inverse of save_baselines_to_disk; rehydrates numpy arrays."""
    with open(path, "r") as f:
        raw = json.load(f)
    out: dict[str, dict] = {}
    for ins, b in raw.items():
        out[ins] = {
            "columns": b["columns"],
            "mean": np.asarray(b["mean"], dtype=float),
            "inv_cov": np.asarray(b["inv_cov"], dtype=float),
            "p30": float(b["p30"]),
            "p70": float(b["p70"]),
            "p95": float(b["p95"]),
        }
    return out


def novelty_multiplier_for_features(
    baseline: dict,
    features_row: pd.Series | dict,
    mode: str = "downscale_only",
    floor: float = 0.5,
    ceiling: float = 1.5,
) -> float:
    """Compute novelty risk multiplier for a single bar given a fitted baseline.

    Returns 1.0 (neutral) if any required feature is missing/non-finite —
    safer than scaling on partial info.
    """
    cols = baseline["columns"]
    if isinstance(features_row, pd.Series):
        x = np.asarray([features_row.get(c, np.nan) for c in cols], dtype=float)
    else:
        x = np.asarray([features_row.get(c, np.nan) for c in cols], dtype=float)
    if not np.all(np.isfinite(x)):
        return 1.0
    diff = x - baseline["mean"]
    score = float(np.sqrt(diff @ baseline["inv_cov"] @ diff))
    if mode == "upscale_familiar":
        return upscale_familiar_multiplier(
            score, baseline["p30"], baseline["p70"], baseline["p95"], ceiling, floor)
    return downscale_only_multiplier(score, baseline["p70"], baseline["p95"], floor)


def fit_per_instrument_baselines(
    ltf_by_name: dict[str, pd.DataFrame],
    train_frac: float = 0.7,
) -> dict[str, dict]:
    """Build the per-instrument baseline dict (suitable for save_baselines_to_disk)."""
    out: dict[str, dict] = {}
    for name, ltf in ltf_by_name.items():
        if ltf is None or ltf.empty:
            continue
        try:
            feats = build_features(ltf)
            cut = int(len(feats) * train_frac)
            out[name] = fit_baseline(feats.iloc[:cut])
        except Exception as e:
            print(f"  [fit_baselines] {name}: skip ({e})")
    return out


def per_instrument_tuned_multipliers(
    ltf_by_name: dict[str, pd.DataFrame],
    train_trades_by_ins: dict,
    train_frac: float = 0.7,
    mode: str = "upscale_familiar",
    candidate_ceil_floor: list[tuple[float, float]] | None = None,
) -> dict[str, pd.Series]:
    """Per-instrument: try several (ceiling, floor) settings, pick the
    one that maximizes weighted-train-period sum-of-R, then apply that
    setting on the full series.

    Cheaper proxy for in-sample optimization than a true grid search:
    we only score the train trades, weight their R by the multiplier at
    that timestamp, sum, and pick the best (ceil, floor) per instrument.
    """
    if candidate_ceil_floor is None:
        candidate_ceil_floor = [(1.5, 0.3), (1.7, 0.3), (1.5, 0.2), (2.0, 0.3), (2.0, 0.2)]

    out: dict[str, pd.Series] = {}
    for name, ltf in ltf_by_name.items():
        if ltf is None or ltf.empty:
            continue
        try:
            feats = build_features(ltf)
            cut = int(len(feats) * train_frac)
            baseline = fit_baseline(feats.iloc[:cut])
        except Exception:
            continue
        scores = score_bars(feats, baseline)

        train_trades = train_trades_by_ins.get(name, [])
        if len(train_trades) < 5:
            # fall back to default
            ceil, fl = 1.5, 0.3
        else:
            best_sum = -float("inf")
            ceil, fl = 1.5, 0.3
            for c, f in candidate_ceil_floor:
                if mode == "upscale_familiar":
                    mult_fn = lambda s: upscale_familiar_multiplier(
                        s, baseline["p30"], baseline["p70"], baseline["p95"], c, f)
                else:
                    mult_fn = lambda s: downscale_only_multiplier(
                        s, baseline["p70"], baseline["p95"], f)
                weighted_sum = 0.0
                for t in train_trades:
                    ts = pd.Timestamp(t.entry_ts)
                    try:
                        if scores.index.tz is not None and ts.tzinfo is None:
                            ts = ts.tz_localize(scores.index.tz)
                        elif scores.index.tz is None and ts.tzinfo is not None:
                            ts = ts.tz_convert(None) if ts.tzinfo else ts
                        pos = scores.index.get_indexer([ts], method="nearest")[0]
                        if pos < 0: continue
                        m = mult_fn(scores.iloc[pos])
                    except Exception:
                        m = 1.0
                    weighted_sum += t.r_multiple_net * m
                if weighted_sum > best_sum:
                    best_sum = weighted_sum
                    ceil, fl = c, f

        # Apply best (ceil, fl) to full series
        if mode == "upscale_familiar":
            mults = scores.apply(lambda s, c=ceil, f=fl: upscale_familiar_multiplier(
                s, baseline["p30"], baseline["p70"], baseline["p95"], c, f))
        else:
            mults = scores.apply(lambda s, c=ceil, f=fl: downscale_only_multiplier(
                s, baseline["p70"], baseline["p95"], f))
        mults.attrs = {"ceiling": ceil, "floor": fl}
        out[name] = mults
    return out
