"""ML regime predictor with walk-forward retraining.

Why predict the *future* regime instead of using the *current* ADX?
  - Current ADX is a lagging indicator: by the time it crosses 25, the
    trend is already 5–15 bars in. We may take chop trades just before
    the trend begins.
  - A model trained on (features now) -> (regime label N bars from now)
    can lean us into trades early and out of trades before chop bites.

Why retrain rolling?
  - Markets shift. A model trained on 2024 data may be stale by mid-2025.
  - Retraining every `refit_every` bars on the most recent `train_window`
    bars keeps the model current — this is the "auto-correction" part.

Method:
  features (X) at bar i = ADX, RSI, ATR ratio, hour sin/cos, day-of-week,
                          recent returns 5/20, recent realized vol 5/20,
                          distance from EMA20 / EMA200 in ATR units.
  target  (y) at bar i = regime label of the median ADX over bars
                          [i+1, i+lookahead] (default lookahead=12).
  model      = HistGradientBoostingClassifier (handles NaN, regularizes
               via shallow trees + l2).
  schedule   = walk forward from `train_window`, fit on
               [i-train_window:i], predict for [i:i+refit_every),
               step by refit_every.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

from features import _adx, _rsi
from strategy import ema, atr
from regime import compute_regime_series


def build_features(ltf: pd.DataFrame) -> pd.DataFrame:
    closes = ltf["close"]
    a14 = atr(ltf, 14)
    a50 = atr(ltf, 50)
    e20 = ema(closes, 20)
    e200 = ema(closes, 200)

    f = pd.DataFrame(index=ltf.index)
    f["adx14"]      = _adx(ltf, 14)
    f["rsi14"]      = _rsi(closes, 14)
    f["atr_ratio"]  = a14 / a50.replace(0, np.nan)
    f["dist_ema20"] = (closes - e20) / a14
    f["dist_ema200"]= (closes - e200) / a14
    f["ret5"]       = closes.pct_change(5)
    f["ret20"]      = closes.pct_change(20)
    f["vol5"]       = closes.pct_change().rolling(5).std()
    f["vol20"]      = closes.pct_change().rolling(20).std()
    hours = ltf.index.hour.values + ltf.index.minute.values / 60.0
    f["hour_sin"]   = np.sin(2 * np.pi * hours / 24)
    f["hour_cos"]   = np.cos(2 * np.pi * hours / 24)
    f["dow"]        = ltf.index.dayofweek.values
    return f


def build_targets(ltf: pd.DataFrame, lookahead: int = 12) -> pd.Series:
    """Regime label of the FUTURE window — what we want to predict."""
    adx = _adx(ltf, 14)
    fwd_med = adx.shift(-1).rolling(lookahead).median().shift(-(lookahead - 1))
    out = pd.Series("neutral", index=ltf.index, dtype=object)
    out[fwd_med >= 25] = "trending"
    out[fwd_med < 20]  = "chop"
    return out


def predict_regime_walk_forward(
    ltf: pd.DataFrame,
    train_window: int = 2000,
    refit_every: int = 200,
    lookahead: int = 12,
    verbose: bool = False,
) -> tuple[pd.Series, dict]:
    """Walk-forward ML regime prediction.

    Returns (predicted_regime_series, metadata).
    The series is None for the warmup window and filled afterward.
    """
    feats = build_features(ltf)
    targets = build_targets(ltf, lookahead=lookahead)

    pred = pd.Series(index=ltf.index, dtype=object)
    pred[:] = None

    n = len(ltf)
    if n < train_window + refit_every:
        return pred, {"error": "not enough data"}

    n_refits = 0
    n_correct = 0
    n_total = 0
    label_hits = {"trending": [0, 0], "neutral": [0, 0], "chop": [0, 0]}  # [correct, total]

    i = train_window
    while i < n:
        win = feats.iloc[i - train_window:i]
        tgt = targets.iloc[i - train_window:i]
        # Drop rows where target is NaN (from the shift at the tail)
        mask = tgt.notna() & (~win.isna().any(axis=1))
        Xtr = win[mask].values
        ytr = tgt[mask].values

        if len(Xtr) < 200:
            i += refit_every
            continue

        model = HistGradientBoostingClassifier(
            max_iter=200, learning_rate=0.05, max_depth=4,
            max_leaf_nodes=15, min_samples_leaf=20,
            l2_regularization=0.1, random_state=42,
        )
        model.fit(Xtr, ytr)
        n_refits += 1

        end = min(i + refit_every, n)
        Xte = feats.iloc[i:end].fillna(0).values
        if len(Xte) > 0:
            yte_pred = model.predict(Xte)
            pred.iloc[i:end] = yte_pred

            # Score against the actual future regime where we know it
            yte_actual = targets.iloc[i:end].values
            for p, a in zip(yte_pred, yte_actual):
                if isinstance(a, str):
                    n_total += 1
                    if p == a:
                        n_correct += 1
                        label_hits[p][0] += 1
                    label_hits[p][1] += 1

        i = end

    accuracy = (n_correct / n_total) if n_total else 0.0
    per_label = {k: (v[0] / v[1] if v[1] else 0.0, v[1]) for k, v in label_hits.items()}
    if verbose:
        print(f"  refits={n_refits} accuracy={accuracy:.3f} per_label={per_label}")

    return pred, {
        "n_refits": n_refits,
        "accuracy": accuracy,
        "per_label_accuracy": per_label,
        "n_predicted": int(pred.notna().sum()),
    }


def build_predicted_regime_lookup(
    ltf_by_name: dict[str, pd.DataFrame],
    train_window: int = 2000,
    refit_every: int = 200,
    lookahead: int = 12,
    verbose: bool = False,
) -> tuple[callable, pd.DataFrame]:
    """Compute predicted-regime series for every instrument and return a
    lookup callable plus an accuracy table."""
    pred_by_name: dict[str, pd.Series] = {}
    rows = []
    for name, ltf in ltf_by_name.items():
        if verbose:
            print(f"  fitting predictor for {name} ...")
        pred, meta = predict_regime_walk_forward(
            ltf, train_window=train_window, refit_every=refit_every,
            lookahead=lookahead, verbose=verbose,
        )
        pred_by_name[name] = pred
        meta_row = {
            "instrument": name,
            "n_refits": meta.get("n_refits", 0),
            "accuracy": meta.get("accuracy", 0.0),
            "n_predicted": meta.get("n_predicted", 0),
        }
        for label, (acc, n) in meta.get("per_label_accuracy", {}).items():
            meta_row[f"acc_{label}"] = acc
            meta_row[f"n_{label}"] = n
        rows.append(meta_row)

    accuracy_df = pd.DataFrame(rows)

    def lookup(name: str, ts) -> str:
        s = pred_by_name.get(name)
        if s is None or s.empty:
            return "neutral"
        t = pd.Timestamp(ts)
        idx = s.index
        if idx.tz is None:
            t = t.tz_localize(None) if t.tzinfo is None else t.tz_convert("UTC").tz_localize(None)
        else:
            t = t.tz_localize(idx.tz) if t.tzinfo is None else t.tz_convert(idx.tz)
        try:
            pos = idx.get_indexer([t], method="nearest")[0]
            if pos < 0:
                return "neutral"
            v = s.iloc[pos]
            return v if isinstance(v, str) else "neutral"
        except Exception:
            return "neutral"

    return lookup, accuracy_df
