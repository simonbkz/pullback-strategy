"""Live regime predictor with on-demand auto-retraining.

Why this module exists:
  `regime_predictor.predict_regime_walk_forward` is for BACKTEST — it walks
  forward through historical data, refitting periodically, and returns a
  historical prediction series. Useful for backtesting; useless for new
  live bars.

  In live, we need to:
    1. Predict the regime for the LATEST bar (which has no historical
       prediction yet).
    2. Re-train periodically as new bars accumulate, so the model stays
       fresh and reflects the most recent regime structure.

  This class keeps a per-instrument sklearn model in memory, refits on a
  time-based schedule (default every 24 hours of wall-clock time since last
  fit), and predicts on the most recent feature row.

Usage:
    mgr = LiveRegimeManager(refit_every_hours=24, train_window=2000, lookahead=12)
    regime = mgr.predict(ins_name, ltf_dataframe)   # 'trending' / 'neutral' / 'chop'
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

from regime_predictor import build_features, build_targets


@dataclass
class _ModelState:
    model: object | None = None
    last_fit: datetime | None = None
    last_n_bars_at_fit: int = 0
    fit_count: int = 0
    columns: list[str] | None = None


@dataclass
class LiveRegimeManager:
    refit_every_hours: float = 24.0
    train_window: int = 2000
    lookahead: int = 12
    min_train_rows: int = 300
    verbose: bool = True
    _state: dict[str, _ModelState] = field(default_factory=dict)

    def _needs_refit(self, ins: str, current_n_bars: int) -> bool:
        st = self._state.get(ins)
        if st is None or st.model is None:
            return True
        if st.last_fit is None:
            return True
        elapsed = datetime.utcnow() - st.last_fit
        if elapsed >= timedelta(hours=self.refit_every_hours):
            return True
        # Also refit if a substantial number of new bars have arrived since last fit
        if current_n_bars - st.last_n_bars_at_fit >= self.train_window // 4:
            return True
        return False

    def _fit(self, ins: str, ltf: pd.DataFrame) -> bool:
        feats = build_features(ltf)
        targets = build_targets(ltf, lookahead=self.lookahead)
        valid = feats.notna().all(axis=1) & targets.notna()
        if valid.sum() < self.min_train_rows:
            if self.verbose:
                print(f"  [regime] {ins}: insufficient training rows ({valid.sum()})")
            return False

        # Use the most recent train_window valid rows
        valid_idx = feats.index[valid]
        recent_idx = valid_idx[-self.train_window:] if len(valid_idx) > self.train_window else valid_idx
        X = feats.loc[recent_idx].fillna(0.0)
        y = targets.loc[recent_idx]

        model = HistGradientBoostingClassifier(
            max_iter=200, learning_rate=0.05, max_depth=4, max_leaf_nodes=15,
            min_samples_leaf=20, l2_regularization=0.1, random_state=42,
        )
        model.fit(X.values, y.values)

        st = self._state.setdefault(ins, _ModelState())
        st.model = model
        st.last_fit = datetime.utcnow()
        st.last_n_bars_at_fit = len(ltf)
        st.fit_count += 1
        st.columns = list(X.columns)
        if self.verbose:
            print(f"  [regime] {ins}: fit #{st.fit_count} on {len(X)} rows  (regimes: {dict(y.value_counts())})")
        return True

    def predict(self, ins: str, ltf: pd.DataFrame) -> str:
        if self._needs_refit(ins, len(ltf)):
            self._fit(ins, ltf)

        st = self._state.get(ins)
        if st is None or st.model is None:
            return "neutral"

        feats = build_features(ltf).iloc[-1:][st.columns].fillna(0.0)
        try:
            return str(st.model.predict(feats.values)[0])
        except Exception:
            return "neutral"

    def force_refit_all(self, ltf_by_name: dict[str, pd.DataFrame]):
        for ins, ltf in ltf_by_name.items():
            self._fit(ins, ltf)

    def status(self) -> pd.DataFrame:
        rows = []
        for ins, st in self._state.items():
            rows.append({
                "instrument": ins,
                "fits": st.fit_count,
                "last_fit_utc": st.last_fit.isoformat() if st.last_fit else None,
                "n_bars_at_fit": st.last_n_bars_at_fit,
                "has_model": st.model is not None,
            })
        return pd.DataFrame(rows)
