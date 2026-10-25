"""Live trader -- wires the Phase 7 research pipeline to a live HFM/MT5 account.

Loop:
  1. Fetch fresh H1 + H4 bars from MT5 for each instrument
  2. Generate strategy signal at the most recently CLOSED H1 bar
  3. Look up predicted regime and compute dynamic risk %
  4. If risk > 0 and no existing position/pending order, place trade
  5. Sleep, repeat

The regime predictor is trained ONCE on startup using the trailing
~2,200+ bars per instrument (warmup_bars in config). Models are not
auto-retrained inside this loop -- the training in regime_predictor.py
is itself walk-forward, so if you stop and restart the trader weekly
the models always train on the latest window. For continuous
auto-correction you can either:
  (a) restart the script weekly via Task Scheduler, or
  (b) set an in-process retrain interval (`refit_every_hours` below).

By design this script does *not* manage break-even, trailing stops, or
partial exits. Once an order has SL+TP attached, the broker manages
exit. This keeps the live system simpler and matches the backtest.
"""
from __future__ import annotations
import argparse
import json
import os
import signal as os_signal
import sys
import time
from datetime import datetime, timedelta
from typing import Any
import numpy as np
import pandas as pd

import MetaTrader5 as mt5

import live_mt5_connector as cx
from config import DEFAULT_PARAMS
from strategy import generate_signals as _pullback_generate_signals
import strategy as _strat_pullback
import strategy_breakout as _strat_breakout
import strategy_momentum as _strat_momentum

# Per-instrument strategy selector. 3-strategy ensemble.
# Default is pullback for any instrument not listed in cfg.strategy_per_instrument.
_STRATEGY_MODULES = {
    "pullback": _strat_pullback,
    "breakout": _strat_breakout,
    "momentum": _strat_momentum,
    # "ensemble" entry is added at module load after _EnsembleStrategy is defined
    # (see line after _EnsembleStrategy class definition below).
}


def _resolve_signal_module(cfg: dict, ins: str):
    per_ins = cfg.get("strategy_per_instrument", {})
    name = per_ins.get(ins, "pullback")
    return _STRATEGY_MODULES.get(name, _strat_pullback)


def _build_params_for_instrument(cfg: dict, ins: str, base_params):
    """Apply per-instrument overrides on top of base params. Currently handles
    vol_regime_filter_per_instrument. Returns a new StrategyParams instance."""
    import dataclasses
    vol_cfg = cfg.get("vol_regime_filter_per_instrument", {})
    if ins in vol_cfg.get("enabled_for", []):
        return dataclasses.replace(
            base_params,
            vol_regime_filter=True,
            atr_norm_window=int(vol_cfg.get("atr_norm_window", 500)),
            atr_norm_min=float(vol_cfg.get("atr_norm_min", 0.6)),
            atr_norm_max=float(vol_cfg.get("atr_norm_max", 2.0)),
        )
    return base_params


def _generate_ensemble_signal(ltf, htf, params):
    """Multi-strategy ensemble: ANY of pullback/breakout/momentum fires = trade.

    Union logic (opposite of _generate_confirmed_signal's 2/3 agreement rule):
      - exactly one signal across the 3 strategies on a bar: take it
      - 2+ in SAME direction (consensus): take first-firing's entry/SL/TP
      - 2+ opposing (conflict): skip (ambiguous bar)

    Backtest validation 2026-10-09 ensemble_bt.py: full-portfolio ensemble
    dilutes edge (-6R vs baseline +41R / 12mo). Per-instrument, 4 clear winners
    (CHFJPY, USDCAD, BTCUSD, ETHUSD) with +47R uplift. Deploy selectively via
    cfg.strategy_per_instrument.<INS> = "ensemble".
    """
    import numpy as np
    sig_p = _strat_pullback.generate_signals(ltf, htf, params)
    sig_b = _strat_breakout.generate_signals(ltf, htf, params)
    sig_m = _strat_momentum.generate_signals(ltf, htf, params)

    out = pd.DataFrame(index=ltf.index)
    out["side"] = pd.Series([None] * len(ltf), dtype=object)
    out["entry"] = np.nan
    out["sl"] = np.nan
    out["tp"] = np.nan
    out["atr_at_signal"] = np.nan

    sides_p = sig_p["side"].values
    sides_b = sig_b["side"].values
    sides_m = sig_m["side"].values
    for i in range(len(ltf)):
        sides = []
        srcs = []
        for s, df in [(sides_p[i], sig_p), (sides_b[i], sig_b), (sides_m[i], sig_m)]:
            if s in ("long", "short"):
                sides.append(s); srcs.append(df)
        if not sides: continue
        if len(set(sides)) > 1: continue  # conflict -> skip
        chosen = srcs[0].iloc[i]
        out.iloc[i, out.columns.get_loc("side")] = chosen["side"]
        out.iloc[i, out.columns.get_loc("entry")] = chosen["entry"]
        out.iloc[i, out.columns.get_loc("sl")] = chosen["sl"]
        out.iloc[i, out.columns.get_loc("tp")] = chosen["tp"]
        out.iloc[i, out.columns.get_loc("atr_at_signal")] = chosen["atr_at_signal"]
    return out


# A shim module exposing generate_signals = _generate_ensemble_signal so
# _STRATEGY_MODULES can treat ensemble like a regular strategy.
class _EnsembleStrategy:
    @staticmethod
    def generate_signals(ltf, htf, params):
        return _generate_ensemble_signal(ltf, htf, params)


# Register ensemble shim into the strategy map now that the class exists.
_STRATEGY_MODULES["ensemble"] = _EnsembleStrategy


def _generate_confirmed_signal(ltf, htf, params):
    """Multi-signal confirmation: require >=2 of 3 strategies to agree on side.
    Uses the first-voting strategy's entry/SL/TP (they usually agree)."""
    import numpy as np
    sig_p = _strat_pullback.generate_signals(ltf, htf, params)
    sig_b = _strat_breakout.generate_signals(ltf, htf, params)
    sig_m = _strat_momentum.generate_signals(ltf, htf, params)

    out = pd.DataFrame(index=ltf.index)
    out["side"] = pd.Series([None] * len(ltf), dtype=object)
    out["entry"] = np.nan
    out["sl"] = np.nan
    out["tp"] = np.nan
    out["atr_at_signal"] = np.nan

    sides_p = sig_p["side"].values
    sides_b = sig_b["side"].values
    sides_m = sig_m["side"].values
    for i in range(len(ltf)):
        votes_long = (sides_p[i] == "long") + (sides_b[i] == "long") + (sides_m[i] == "long")
        votes_short = (sides_p[i] == "short") + (sides_b[i] == "short") + (sides_m[i] == "short")
        chosen = None
        if votes_long >= 2:
            for sig in (sig_p, sig_b, sig_m):
                if sig.iloc[i]["side"] == "long":
                    chosen = sig.iloc[i]; break
        elif votes_short >= 2:
            for sig in (sig_p, sig_b, sig_m):
                if sig.iloc[i]["side"] == "short":
                    chosen = sig.iloc[i]; break
        if chosen is not None:
            out.iloc[i, out.columns.get_loc("side")] = chosen["side"]
            out.iloc[i, out.columns.get_loc("entry")] = chosen["entry"]
            out.iloc[i, out.columns.get_loc("sl")] = chosen["sl"]
            out.iloc[i, out.columns.get_loc("tp")] = chosen["tp"]
            out.iloc[i, out.columns.get_loc("atr_at_signal")] = chosen["atr_at_signal"]
    return out


_ML_MODELS: dict | None = None  # loaded lazily on first use


def _load_ml_models():
    """Load joblib model files from ml_models/ directory. Returns dict:
    (instrument, strategy_name) -> {model, scaler, threshold, features}."""
    global _ML_MODELS
    if _ML_MODELS is not None:
        return _ML_MODELS
    _ML_MODELS = {}
    try:
        import joblib
        import json as _json
        model_dir = __import__("pathlib").Path("ml_models")
        if not model_dir.exists():
            return _ML_MODELS
        for p in model_dir.glob("*.joblib"):
            stem = p.stem  # e.g., "BTCUSD_breakout"
            parts = stem.rsplit("_", 1)
            if len(parts) != 2: continue
            ins, strat = parts
            try:
                bundle = joblib.load(p)
                thr_file = model_dir / f"{stem}.thr.json"
                thr = 0.5
                if thr_file.exists():
                    thr = float(_json.loads(thr_file.read_text()).get("threshold", 0.5))
                _ML_MODELS[(ins, strat)] = {
                    "model": bundle["model"],
                    "scaler": bundle.get("scaler"),
                    "features": bundle.get("features"),
                    "threshold": thr,
                }
                print(f"[ml_rescue] loaded model: {ins}_{strat} (threshold={thr:.4f})")
            except Exception as e:
                print(f"[ml_rescue] failed to load {p.name}: {e}")
    except Exception as e:
        print(f"[ml_rescue] module load error: {e}")
    return _ML_MODELS


def _ml_rescue_skip(cfg: dict, ins: str, strat_name: str, ltf, htf) -> tuple[bool, str]:
    """Skip entry if ML model predicts loss. No-op if (ins, strat) model not present."""
    blk = cfg.get("ml_rescue", {})
    if not blk.get("enabled", False):
        return False, ""
    if not blk.get("per_instrument", {}).get(ins, False):
        return False, ""
    models = _load_ml_models()
    key = (ins, strat_name)
    if key not in models:
        return False, ""
    try:
        from _ml_features import build_features
        feat = build_features(ltf, htf)
        feat = feat[~feat.index.duplicated(keep="last")]
        if len(feat) < 2:
            return False, ""
        row = feat.iloc[-2]  # last fully-closed bar
        fcols = models[key]["features"]
        X = row[fcols].values.reshape(1, -1)
        if np.any(np.isnan(X)):
            return False, ""
        model = models[key]["model"]
        scaler = models[key]["scaler"]
        X_s = scaler.transform(X) if scaler is not None else X
        # RF: predict_proba works on raw; LR on scaled. Try raw first, fall back.
        try:
            prob = float(model.predict_proba(X)[:, 1][0])
        except Exception:
            prob = float(model.predict_proba(X_s)[:, 1][0])
        thr = models[key]["threshold"]
        if prob < thr:
            return True, f"ml_rescue_skip(prob={prob:.3f}<thr={thr:.3f}, strat={strat_name})"
    except Exception as e:
        print(f"  [{ins}] ml_rescue error: {e}")
    return False, ""


def _h4_adx(htf, period: int = 14):
    """ADX on HTF bars. Returns the last value or None if insufficient data."""
    if len(htf) < period * 3:
        return None
    h, l, c = htf["high"], htf["low"], htf["close"]
    up = h.diff()
    down = -l.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=htf.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=htf.index)
    tr = pd.concat([(h - l).abs(), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atrv = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1/period, adjust=False).mean() / atrv
    minus_di = 100 * minus_dm.ewm(alpha=1/period, adjust=False).mean() / atrv
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
    adx = dx.ewm(alpha=1/period, adjust=False).mean()
    if len(adx) == 0:
        return None
    val = adx.iloc[-2] if len(adx) >= 2 else adx.iloc[-1]  # last fully-closed bar
    return float(val) if np.isfinite(val) else None


def _strong_regime_skip(cfg: dict, htf) -> tuple[bool, str]:
    """Skip entry when H4 ADX exceeds threshold. Backtest 24mo/36mo/12mo
    showed +0.02 to +0.04R meanR uplift from skipping strong-regime entries
    across all 3 strategies, at ~30% trade reduction."""
    blk = cfg.get("regime_filter", {})
    if not blk.get("enabled", False):
        return False, ""
    threshold = float(blk.get("adx_threshold", 30.0))
    period = int(blk.get("adx_period", 14))
    adx_val = _h4_adx(htf, period)
    if adx_val is None:
        return False, ""
    if adx_val >= threshold:
        return True, f"strong_regime_filter(adx={adx_val:.1f}>={threshold})"
    return False, ""


def _get_entry_signal(cfg: dict, ins: str, ltf, htf, base_params):
    """Generate the signal frame used for entry decisions. Applies per-instrument
    vol filter override, and optionally multi-signal confirmation gate."""
    params = _build_params_for_instrument(cfg, ins, base_params)
    msc = cfg.get("multi_signal_confirmation", {})
    if msc.get("enabled", False) and msc.get("per_instrument", {}).get(ins, False):
        return _generate_confirmed_signal(ltf, htf, params)
    sig_module = _resolve_signal_module(cfg, ins)
    return sig_module.generate_signals(ltf, htf, params)


# Back-compat alias for any code that imported generate_signals from live_trader.
generate_signals = _pullback_generate_signals
from regime import compute_regime_series
from regime_predictor import build_predicted_regime_lookup, build_features
from live_regime_manager import LiveRegimeManager
from risk_scaling import dynamic_risk
from regime_novelty import load_baselines_from_disk, novelty_multiplier_for_features
import trade_journal as journal


# ---- runtime state ------------------------------------------------------
RUNTIME_STATE_FILE = "runtime_state.json"


class State:
    def __init__(self):
        self.last_bar_seen: dict[str, pd.Timestamp] = {}
        self.equity_peak: float = 0.0
        self.startup_time: datetime = datetime.now(__import__("datetime").timezone.utc)
        self.predict_regime: Any = None        # callable(ins, ltf_df) -> regime string
        self.now_regime:     Any = None        # callable(ins, ts)     -> regime string
        self.regime_manager: LiveRegimeManager | None = None
        self.broker_symbols: dict[str, str] = {}
        self.last_heartbeat: datetime = datetime.now(__import__("datetime").timezone.utc)
        # Delayed-SL bookkeeping: ticket -> {planned_sl, tp, swap_at_unix, ins, broker}
        self.pending_sl_swap: dict[int, dict] = {}
        self.chop_streak: dict[str, int] = {}   # per-instrument consecutive 'chop' bars
        # Per-position metadata captured at open: planned_dist + side, used by
        # break-even trail and any future SL-management logic that needs to
        # know the trade's original 1R risk.
        self.position_meta: dict[int, dict] = {}
        self.be_armed: set[int] = set()         # tickets where BE has already been moved
        self.novelty_baselines: dict[str, dict] | None = None  # per-instrument Mahalanobis baselines (loaded at startup if novelty.enabled)
        # Profit ratchet: cumulative banked equity. Risk is computed off
        # (eq - banked_equity), so once gains are banked they don't compound
        # back into trade size. Restart-safe via runtime state.
        self.banked_equity: float = 0.0
        # Starting equity for the trader instance (first observed equity).
        # Used as the ratchet baseline. 0.0 until first poll.
        self.start_equity: float = 0.0
        # Consecutive-loss streak counter for the loss-streak throttle.
        # Increments on every closed losing trade, resets on any winner.
        # Restart-safe via runtime state.
        self.consecutive_losses: int = 0

    def save_runtime(self):
        try:
            data = {
                "pending_sl_swap": {
                    str(tid): {**v} for tid, v in self.pending_sl_swap.items()
                },
                "position_meta": {
                    str(tid): {**v} for tid, v in self.position_meta.items()
                },
                "be_armed": sorted(self.be_armed),
                "equity_peak": float(self.equity_peak),
                "banked_equity": float(self.banked_equity),
                "start_equity": float(self.start_equity),
                "consecutive_losses": int(self.consecutive_losses),
                "saved_at": datetime.now(__import__("datetime").timezone.utc).isoformat(),
            }
            with open(RUNTIME_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[runtime] save failed: {e}")

    def load_runtime(self):
        if not os.path.exists(RUNTIME_STATE_FILE): return
        try:
            with open(RUNTIME_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.pending_sl_swap = {
                int(tid): v for tid, v in data.get("pending_sl_swap", {}).items()
            }
            self.position_meta = {
                int(tid): v for tid, v in data.get("position_meta", {}).items()
            }
            self.be_armed = set(int(t) for t in data.get("be_armed", []))
            saved_peak = float(data.get("equity_peak", 0.0) or 0.0)
            if saved_peak > self.equity_peak:
                self.equity_peak = saved_peak
            self.banked_equity = float(data.get("banked_equity", 0.0) or 0.0)
            self.start_equity = float(data.get("start_equity", 0.0) or 0.0)
            self.consecutive_losses = int(data.get("consecutive_losses", 0) or 0)
            print(f"[runtime] loaded {len(self.pending_sl_swap)} pending SL-swap entries, "
                  f"{len(self.position_meta)} position-meta records, "
                  f"{len(self.be_armed)} be-armed tickets, "
                  f"equity_peak=${self.equity_peak:,.2f}, "
                  f"banked=${self.banked_equity:,.2f}, start=${self.start_equity:,.2f}")
        except Exception as e:
            print(f"[runtime] load failed: {e}")


# ---- helpers ------------------------------------------------------------
def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _log_signal(ts, ins, side, entry, sl, tp, planned_dist, decision,
                 reason: str | None = None, ticket: int | None = None) -> None:
    """Persist a signal-considered event for counterfactual analysis.
    decision='taken' or 'skipped'. reason is short slug ('blackout_hour',
    'news_NFP', 'risk_overshoot', 'heat_cap', 'min_lot_undersize',
    'risk_zero', 'lot_zero'). Failures are swallowed -- never break trading."""
    try:
        journal.log_signal(
            ts=ts, instrument=ins, side=side,
            entry_price=entry, sl=sl, tp=tp,
            planned_dist=planned_dist,
            decision=decision, skip_reason=reason, ticket=ticket,
        )
    except Exception as e:
        # Defensive: logging must not break the trader.
        print(f"  [signals] log failed: {e}")


def _h1_mae_mfe_since(p_time_unix: float, entry_price: float, side: str,
                       broker_symbol: str) -> tuple[float, float, int]:
    """Compute (MAE_pts, MFE_pts, bars_elapsed) from entry to now using H1 bars.

    MAE = max adverse excursion in price pts (positive magnitude).
    MFE = max favorable excursion in price pts (positive magnitude).

    For 'long': adverse = entry - low; favorable = high - entry.
    For 'short': adverse = high - entry; favorable = entry - low.

    Returns (0.0, 0.0, 0) on any failure (fail-open: don't act on bad data).
    """
    try:
        # position.time is a BROKER-time epoch (GMT+2/+3); copy_rates_range
        # compares against broker-time bar labels, so the range END must be
        # expressed in broker time too. Before 2026-10-22 it was real UTC, which
        # cut the window 3h short: bars undercounted by 3 and MAE/MFE ignored
        # the last 3 hours of price.
        _tz = __import__("datetime").timezone.utc
        entry_dt = datetime.fromtimestamp(float(p_time_unix), tz=_tz)
        now_dt = datetime.now(tz=_tz) + timedelta(seconds=cx._get_broker_offset())
        rates = mt5.copy_rates_range(broker_symbol, mt5.TIMEFRAME_H1, entry_dt, now_dt)
        if rates is None or len(rates) == 0:
            return 0.0, 0.0, 0
        side = (side or "").lower()
        mae = 0.0; mfe = 0.0
        for r in rates:
            hi = float(r["high"]); lo = float(r["low"])
            if side == "long":
                mae = max(mae, entry_price - lo)
                mfe = max(mfe, hi - entry_price)
            else:
                mae = max(mae, hi - entry_price)
                mfe = max(mfe, entry_price - lo)
        bars = max(0, len(rates) - 1)
        return max(0.0, mae), max(0.0, mfe), bars
    except Exception:
        return 0.0, 0.0, 0


def _pos_age_seconds(p) -> float:
    """Seconds since a position opened, in a consistent time base.

    position.time is a BROKER-time epoch (GMT+2/+3). Comparing it with
    time.time() (real UTC) made every age negative for the first offset-hours:
    the scale-in min_age gate (60 s) was effectively a 3-hour gate, and the
    early_adverse_cut gate only worked because it tolerated negatives
    (found 2026-10-23). Compare in broker time instead.
    """
    try:
        return (time.time() + cx._get_broker_offset()) - float(p.time)
    except Exception:
        return 0.0


def _h1_bars_since(p_time_unix: float, broker_symbol: str) -> int:
    """Count actual H1 bars from a position's open time to NOW using MT5 history.

    This is CRITICAL for live-vs-backtest alignment of any time-based logic
    (decay_sl, stale_exit). Wall-clock hours overcount during weekends and
    market closures: an FX trade opened Friday 21:00 will be 61 wall hours
    old by Monday 10:00 but only ~13 actual H1 bars have closed (FX market
    closed Fri 22:00 -> Sun 22:00). Counting wall hours would apply ~5x
    more decay than backtest. This function uses copy_rates_range to count
    only bars that actually exist in the broker's H1 series.

    Returns 0 on any failure (fail-open: better to under-decay than crash).
    """
    try:
        # See _h1_mae_mfe_since: range end must be in broker time (2026-10-22 fix;
        # previously decay_sl / stale_exit / early_adverse_cut ran 3 bars late).
        _tz = __import__("datetime").timezone.utc
        entry_dt = datetime.fromtimestamp(float(p_time_unix), tz=_tz)
        now_dt = datetime.now(tz=_tz) + timedelta(seconds=cx._get_broker_offset())
        rates = mt5.copy_rates_range(broker_symbol, mt5.TIMEFRAME_H1, entry_dt, now_dt)
        if rates is None or len(rates) == 0:
            return 0
        # rates includes the entry bar; subtract 1 to get "bars elapsed since open"
        return max(0, len(rates) - 1)
    except Exception:
        return 0


def _portfolio_open_risk_pct(cfg: dict, magic: int, eq: float) -> float:
    """Sum of unrealized worst-case risk across all currently-open positions
    expressed as % of equity. Each open position's worst case = abs(entry - SL)
    converted to USD via order_calc_profit, divided by equity.

    Returns 0.0 if no open positions or any computation fails (fail-open is
    safer than blocking on a transient broker hiccup, though we log a warning)."""
    try:
        positions = mt5.positions_get() or []
    except Exception:
        return 0.0
    ours = [p for p in positions if p.magic == magic]
    if not ours or eq <= 0:
        return 0.0
    total = 0.0
    for p in ours:
        if not p.sl:
            continue
        order_type = mt5.ORDER_TYPE_BUY if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_SELL
        try:
            loss = mt5.order_calc_profit(order_type, p.symbol, p.volume, p.price_open, p.sl)
            if loss is not None and loss < 0:
                total += -loss
        except Exception:
            continue
    return total / eq * 100.0


def _in_bad_dow(cfg: dict, ts) -> tuple[bool, str]:
    """Day-of-week filter -- block entries on historically bad days (UTC).

    Journal analysis 2026-10-08 found Tuesday meanR = -0.491R across 13
    trades since 2026-07-01 (biggest realized $ loss day: -$686). Monday
    meanR -0.230R (n=21) but $ impact small (-$6). Blocking Tuesday
    would have avoided ~$1,300 of losses at current risk level.

    No-op unless cfg.dow_filter.enabled. Uses `blocked_days` list of
    integers (0=Mon ... 6=Sun) or day name strings.
    """
    df = cfg.get("dow_filter", {})
    if not df.get("enabled", False):
        return False, ""
    raw = df.get("blocked_days", [])
    if not raw:
        return False, ""
    DAYS = {"mon":0,"tue":1,"wed":2,"thu":3,"fri":4,"sat":5,"sun":6}
    blocked = set()
    for d in raw:
        if isinstance(d, int): blocked.add(int(d))
        elif isinstance(d, str): blocked.add(DAYS.get(d.lower()[:3], -1))
    blocked.discard(-1)
    if not blocked: return False, ""
    try:
        d = int(ts.weekday())
    except Exception:
        return False, ""
    if d in blocked:
        names = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
        return True, f"bad_dow_{names[d]}"
    return False, ""


def _in_bad_hour(cfg: dict, ts) -> tuple[bool, str]:
    """Hour-of-day filter -- block entries during historically bad hours (UTC).

    Journal analysis 2026-10-07 showed Asian session (23-05 UTC) runs at
    -0.445R meanR across 20 trades over last 3 months, dominated by thin
    liquidity and wide overnight spreads. Blocking those hours would have
    saved ~$2.7K of realized losses across 97 trades.

    No-op unless cfg.hour_filter.enabled. Uses `blocked_hours_utc` list.
    Returns (True, reason) if gated, (False, '') otherwise.
    """
    hf = cfg.get("hour_filter", {})
    if not hf.get("enabled", False):
        return False, ""
    blocked = set(int(h) for h in hf.get("blocked_hours_utc", []))
    if not blocked:
        return False, ""
    try:
        h = int(ts.hour)
    except Exception:
        return False, ""
    if h in blocked:
        return True, f"bad_hour_{h:02d}UTC"
    return False, ""


def _should_auto_exclude(cfg: dict, ins: str) -> tuple[bool, str]:
    """Auto-exclude an instrument whose recent live expectancy is clearly bad
    AND whose signal frequency has collapsed. Catches "strategy choice is
    wrong" cases that regime pulse flags daily but requires manual action.

    Rule: last N trades meanR < threshold AND dormant_days > min_dormant.
    AND-gate (not OR): a losing-but-active instrument may still be in a
    temporary rough patch; a losing-and-dormant one has a structural mismatch
    (strategy signal rarely fires + what fires loses) and should pause.

    No-op unless cfg.auto_exclusion.enabled. Caches result per instrument
    for 1 hour to avoid hammering the journal on every poll.
    """
    ae = cfg.get("auto_exclusion", {})
    if not ae.get("enabled", False):
        return False, ""
    lookback = int(ae.get("lookback_trades", 15))
    threshold = float(ae.get("meanR_threshold", -0.15))
    dormant_days = int(ae.get("dormant_days", 14))
    min_sample = int(ae.get("min_sample", 10))

    # Lightweight cache: cfg mutation keeps it per-process. 1h TTL.
    cache = _AE_CACHE
    now = time.time()
    cached = cache.get(ins)
    if cached and (now - cached[0] < 3600):
        return cached[1], cached[2]

    try:
        import sqlite3 as _sqlite3
        _db = _sqlite3.connect("trade_journal.db")
        rows = _db.execute(
            "SELECT r_multiple_gross, entry_ts FROM trades "
            "WHERE instrument = ? AND r_multiple_gross IS NOT NULL "
            "AND (notes IS NULL OR notes NOT LIKE 'scale_in_add%') "
            "ORDER BY entry_ts DESC LIMIT ?",
            (ins, lookback),
        ).fetchall()
        _db.close()
    except Exception:
        cache[ins] = (now, False, "")
        return False, ""

    if len(rows) < min_sample:
        cache[ins] = (now, False, "")
        return False, ""

    rs = [r[0] for r in rows]
    meanR = sum(rs) / len(rs)
    if meanR >= threshold:
        cache[ins] = (now, False, "")
        return False, ""

    try:
        last_ts = datetime.fromisoformat(rows[0][1].replace("Z", "+00:00"))
        days_since = (datetime.now(tz=__import__("datetime").timezone.utc)
                       - last_ts).total_seconds() / 86400
    except Exception:
        cache[ins] = (now, False, "")
        return False, ""

    if days_since < dormant_days:
        cache[ins] = (now, False, "")
        return False, ""

    reason = f"auto_excl(meanR={meanR:+.3f}R,n={len(rs)},dormant={int(days_since)}d)"
    # Re-admission (2026-10-24): exclusion is otherwise a one-way door -- the
    # instrument can't trade, so its meanR can never recover. If the last 10
    # journaled auto_excl skips have been counterfactually scored and would
    # have been net positive, let it trade again (the expectancy throttle still
    # halves/pauses it on real results). Purely journal-driven; no new state.
    try:
        import sqlite3 as _sq
        _db = _sq.connect("trade_journal.db")
        cf = [r[0] for r in _db.execute(
            "SELECT cf_r_multiple FROM signals WHERE instrument=? AND skip_reason='auto_excl' "
            "AND cf_r_multiple IS NOT NULL ORDER BY ts DESC LIMIT 10", (ins,))]
        _db.close()
        if len(cf) >= 10 and sum(cf) / len(cf) > 0:
            reason = f"auto_excl_readmit(cfR={sum(cf)/len(cf):+.2f} over {len(cf)} skips)"
            cache[ins] = (now, False, reason)
            return False, reason
    except Exception:
        pass
    cache[ins] = (now, True, reason)
    return True, reason


_AE_CACHE: dict = {}


_NEWS_CACHE: dict = {"mtime": None, "block": None}


def _live_news_block(cfg: dict) -> dict:
    """Return cfg.news_blackout, re-read from disk when the config file changes.

    news_feed.py rewrites mt5_runtime.json every morning with the coming
    week's windows, but the trader loads config once at startup, so the
    in-memory windows went stale after ~a week without a restart (audit
    finding 2026-10-23). Only this block is hot-reloaded; everything else
    still requires a restart. Falls back to the in-memory block on any error.
    """
    path = cfg.get("_config_path") or "mt5_runtime.json"
    try:
        mtime = os.path.getmtime(path)
        if _NEWS_CACHE["mtime"] != mtime:
            with open(path, "r", encoding="utf-8") as f:
                blk = json.load(f).get("news_blackout", {})
            if _NEWS_CACHE["mtime"] is not None:
                print(f"[news] reloaded news_blackout from disk ({len(blk.get('windows', []))} windows)")
            _NEWS_CACHE["mtime"] = mtime; _NEWS_CACHE["block"] = blk
        return _NEWS_CACHE["block"] or cfg.get("news_blackout", {})
    except Exception:
        return cfg.get("news_blackout", {})


def _in_news_blackout(cfg: dict, ts, ins: str | None = None) -> tuple[bool, str]:
    """Check if `ts` (UTC) falls inside any configured news-window blackout
    that applies to the given instrument.

    Config: cfg.news_blackout = {
      "enabled": bool,
      "windows": [
        {"name": "NFP", "weekday": 4, "ordinal": 1, "utc_start": "13:25", "utc_end": "14:30",
         "applies_to": ["XAUUSD", "US500", "US100", "USOIL"]},
        ...
      ]
    }

    A window with no `applies_to` (or empty list) applies to ALL instruments
    (legacy behavior). With `applies_to`, only listed instruments are affected.
    Pass ins=None to check "would ANY window apply" (legacy callers).
    """
    nb = _live_news_block(cfg)
    if not nb.get("enabled", False):
        return (False, "")
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    for w in nb.get("windows", []):
        # Per-asset applicability filter
        applies_to = w.get("applies_to") or []
        if applies_to and ins is not None and ins not in applies_to:
            continue
        s_h, s_m = [int(x) for x in w["utc_start"].split(":")]
        e_h, e_m = [int(x) for x in w["utc_end"].split(":")]
        # Date-list match
        dates = w.get("dates", [])
        if dates and t.strftime("%Y-%m-%d") in dates:
            ws = t.normalize() + pd.Timedelta(hours=s_h, minutes=s_m)
            we = t.normalize() + pd.Timedelta(hours=e_h, minutes=e_m)
            if ws <= t <= we:
                return (True, w.get("name", "news"))
            continue
        # Weekday/ordinal pattern match
        wd = w.get("weekday")
        if wd is not None and t.weekday() == wd:
            ord_n = w.get("ordinal")
            if ord_n is not None:
                # nth weekday-of-month: count occurrences from day 1
                day_of_month = t.day
                if (day_of_month - 1) // 7 + 1 != ord_n:
                    continue
            ws = t.normalize() + pd.Timedelta(hours=s_h, minutes=s_m)
            we = t.normalize() + pd.Timedelta(hours=e_h, minutes=e_m)
            if ws <= t <= we:
                return (True, w.get("name", "news"))
    return (False, "")


def _in_session_blackout(cfg: dict, ins: str, ts) -> tuple[bool, str]:
    """Per-asset session filter. Returns (in_blackout, session_name).

    Config: cfg.session_filter_per_instrument = {
      "enabled": bool,
      "hours": {
        "DE40":  [7,8,9,10,11,12,13,14,15,16,17,18,19,20],
        "JP225": [23,0,1,2,3,4,5,6],
        ...
      }
    }
    Hour list is the ALLOWED UTC hours. Outside the list -> blocked.
    """
    sf = cfg.get("session_filter_per_instrument", {})
    if not sf.get("enabled", False):
        return (False, "")
    hours_map = sf.get("hours", {})
    allowed = hours_map.get(ins)
    if not allowed:
        return (False, "")  # no rule for this instrument = no filter
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    if t.hour in allowed:
        return (False, "")
    return (True, f"out_of_session_{ins}")


def _correlation_scale(cfg: dict, ins: str, side: str, magic: int) -> tuple[float, str]:
    """Correlation-aware sizing. Returns a scale multiplier for position risk.

    Modes (cfg.correlation_skip.mode):
      "skip"  -- return 0.0 if a same-direction correlated position is open
                 (binary block, original behavior)
      "scale" -- return cfg.correlation_skip.scale_factor (default 0.5) if
                 correlated same-direction is open, else 1.0. Lets the trade
                 happen but at reduced size to cap concentration risk.

    Data (2026-07-02 audit): 5 US500+US100 same-direction overlaps totaled
    -5.21R -- fake diversification. Scale mode captures the opportunity at
    reduced risk instead of skipping entirely."""
    blk = cfg.get("correlation_skip", {})
    if not blk.get("enabled", False):
        return 1.0, ""
    mode = blk.get("mode", "skip")
    scale_factor = float(blk.get("scale_factor", 0.5))
    groups = blk.get("groups", [])
    matching_group = None
    for g in groups:
        if ins in g:
            matching_group = g
            break
    if not matching_group:
        return 1.0, ""
    positions = mt5.positions_get() or []
    symbol_map = cfg.get("symbol_map", {})
    for p in positions:
        if p.magic != magic:
            continue
        p_ins = _reverse_logical_name(p.symbol, symbol_map)
        if p_ins is None or p_ins == ins:
            continue
        if p_ins not in matching_group:
            continue
        p_side = "long" if p.type == mt5.POSITION_TYPE_BUY else "short"
        if p_side == side:
            if mode == "scale":
                return scale_factor, f"correlation_scale({scale_factor}x): {p_ins} {p_side} open (group {matching_group})"
            return 0.0, f"correlation_skip: {p_ins} {p_side} already open (group {matching_group})"
    return 1.0, ""


# Back-compat alias: some older call sites use _correlation_skip returning (bool, str).
def _correlation_skip(cfg: dict, ins: str, side: str, magic: int) -> tuple[bool, str]:
    scale, reason = _correlation_scale(cfg, ins, side, magic)
    return (scale == 0.0), reason


def _inst_expectancy_scale(cfg: dict, ins: str) -> tuple[float, str]:
    """Per-instrument rolling expectancy throttle.

    Computes mean R-multiple of the last `lookback_trades` closed trades on
    this instrument. If it's deeply negative, halve risk or pause entries
    entirely. Direct response to regime shifts where one instrument decouples
    from the rest of the basket.

    Faster + more granular than V_INST_DD (which only fires at 5R cumulative
    drawdown -- can take many trades). This catches a per-instrument bleed
    after just `min_trades` (default 5) bad results.

    Returns (scale, reason). 1.0 / no-op when disabled, when journal missing,
    or when fewer than min_trades closed trades exist.
    """
    cfg_blk = cfg.get("inst_expectancy_throttle", {})
    if not cfg_blk.get("enabled", False):
        return 1.0, ""
    # Exempt list: instruments whose historical R comes from a bug-affected
    # window get a fresh start. Added 2026-07-05 after H4 TZ bug fix so
    # backtest-strong instruments (JP225 +0.51R, USOIL +0.47R) aren't held
    # hostage by pre-fix losses.
    if ins in cfg_blk.get("exempt_instruments", []):
        return 1.0, f"inst_expectancy_exempt({ins})"
    min_trades = int(cfg_blk.get("min_trades", 5))
    lookback = int(cfg_blk.get("lookback_trades", 10))
    throttle_thr = float(cfg_blk.get("throttle_threshold_R", -0.1))
    throttle_scale = float(cfg_blk.get("throttle_scale", 0.5))
    pause_thr = float(cfg_blk.get("pause_threshold_R", -0.3))
    # min_trade_date_utc: only count trades whose exit_ts >= this ISO date.
    # Added 2026-08-16 for fresh-start throttle window after config resets so
    # pre-fix bug-era losses don't gate current trading indefinitely.
    min_date = cfg_blk.get("min_trade_date_utc")
    try:
        import sqlite3
        db = sqlite3.connect("trade_journal.db")
        if min_date:
            # scale-in adds excluded (2026-10-22): an add is conditional on the
            # parent already being +1R, so counting it double-counts one move.
            cur = db.execute(
                "SELECT r_multiple_gross FROM trades WHERE instrument=? "
                "AND exit_ts IS NOT NULL AND r_multiple_gross IS NOT NULL "
                "AND (notes IS NULL OR notes NOT LIKE 'scale_in_add%') "
                "AND exit_ts >= ? "
                "ORDER BY exit_ts DESC LIMIT ?", (ins, min_date, lookback))
        else:
            cur = db.execute(
                "SELECT r_multiple_gross FROM trades WHERE instrument=? "
                "AND exit_ts IS NOT NULL AND r_multiple_gross IS NOT NULL "
                "AND (notes IS NULL OR notes NOT LIKE 'scale_in_add%') "
                "ORDER BY exit_ts DESC LIMIT ?", (ins, lookback))
        rs = [float(r[0]) for r in cur.fetchall()]
        db.close()
    except Exception:
        return 1.0, ""
    if len(rs) < min_trades:
        return 1.0, ""
    mean_r = sum(rs) / len(rs)

    # ---- Statistical rigor on PAUSE (added 2026-10-11) ----
    # Pause (×0) is costly: an instrument stops trading entirely. Require BOTH
    # effect-size (meanR below pause_thr) AND statistical significance
    # (p-value from one-sample t-test on R-multiples vs H0: meanR=0) before
    # pausing. If effect-size says pause but p-value is not significant,
    # fall back to the softer throttle (×0.5) — don't shut off entirely on
    # noise.  Throttle itself remains heuristic because it's reversible on
    # the next winner.
    pause_p_max = float(cfg_blk.get("pause_p_value_max", 0.10))
    if mean_r <= pause_thr:
        # Compute one-sample t-stat vs 0 (left-tailed; we want p = P(T <= t))
        if len(rs) >= 2:
            try:
                import statistics
                sd = statistics.pstdev(rs) if len(rs) == 1 else statistics.stdev(rs)
            except Exception:
                sd = 0.0
        else:
            sd = 0.0
        p_val = None
        if sd > 1e-9 and len(rs) >= 2:
            try:
                from scipy import stats as _sstats
                t_stat = mean_r / (sd / (len(rs) ** 0.5))
                p_val = float(_sstats.t.cdf(t_stat, df=len(rs) - 1))
            except Exception:
                p_val = None
        if p_val is not None and p_val <= pause_p_max:
            return 0.0, (f"inst_expectancy_pause(meanR={mean_r:+.2f}<={pause_thr}, "
                         f"n={len(rs)}, p={p_val:.3f}<={pause_p_max})")
        # Effect-size met but not statistically significant -> throttle only
        return throttle_scale, (f"inst_expectancy_throttle(meanR={mean_r:+.2f}<={pause_thr} "
                                 f"but p={p_val if p_val is None else round(p_val,3)}>pause_p_max, "
                                 f"scale={throttle_scale}, n={len(rs)})")
    if mean_r <= throttle_thr:
        return throttle_scale, f"inst_expectancy_throttle(meanR={mean_r:+.2f}<={throttle_thr}, scale={throttle_scale}, n={len(rs)})"
    # *** Asymmetric leverage boost on positive-edge instruments
    # (Plan-agent recommendation 2026-06-05). ETHUSD/JP225/US500 have
    # proven edge in live -- concentrate capital on them when the regime
    # validates it. Stacks with portfolio_regime_boost.
    boost_lo_thr = float(cfg_blk.get("boost_lo_threshold_R", 0.2))
    boost_lo_scale = float(cfg_blk.get("boost_lo_scale", 1.25))
    boost_hi_thr = float(cfg_blk.get("boost_hi_threshold_R", 0.5))
    boost_hi_scale = float(cfg_blk.get("boost_hi_scale", 1.5))
    if mean_r >= boost_hi_thr:
        return boost_hi_scale, f"inst_expectancy_BOOST_HI(meanR={mean_r:+.2f}>={boost_hi_thr}, scale={boost_hi_scale}, n={len(rs)})"
    if mean_r >= boost_lo_thr:
        return boost_lo_scale, f"inst_expectancy_boost(meanR={mean_r:+.2f}>={boost_lo_thr}, scale={boost_lo_scale}, n={len(rs)})"
    return 1.0, ""


def _portfolio_regime_scale(cfg: dict) -> tuple[float, str]:
    """Bidirectional portfolio-wide expectancy throttle.

    Downside: cut risk when basket expectancy goes negative (catches regime
    shift before DD circuit breaker fires).

    Upside (boost_*): scale UP risk when basket expectancy is strongly
    positive. Composes with the existing protection stack -- chandelier still
    runs winners, decay still cuts losers, just at larger position sizes.
    Active growth lever.

    Returns scale; thresholds checked in order: pause -> throttle -> boost_hi
    -> boost_lo -> 1.0.
    """
    cfg_blk = cfg.get("portfolio_regime_throttle", {})
    if not cfg_blk.get("enabled", False):
        return 1.0, ""
    lookback = int(cfg_blk.get("lookback_trades", 20))
    min_trades = int(cfg_blk.get("min_trades", 10))
    throttle_thr = float(cfg_blk.get("throttle_threshold_R", -0.2))
    throttle_scale = float(cfg_blk.get("throttle_scale", 0.5))
    pause_thr = float(cfg_blk.get("pause_threshold_R", -0.5))
    boost_lo_thr = float(cfg_blk.get("boost_lo_threshold_R", 0.2))
    boost_lo_scale = float(cfg_blk.get("boost_lo_scale", 1.2))
    boost_hi_thr = float(cfg_blk.get("boost_hi_threshold_R", 0.4))
    boost_hi_scale = float(cfg_blk.get("boost_hi_scale", 1.5))
    # Same min_trade_date_utc mechanism as inst_expectancy_throttle: fresh
    # observation window that ignores pre-fix bug-era history.
    min_date = cfg_blk.get("min_trade_date_utc")
    try:
        import sqlite3
        db = sqlite3.connect("trade_journal.db")
        if min_date:
            cur = db.execute(
                "SELECT r_multiple_gross FROM trades WHERE exit_ts IS NOT NULL "
                "AND r_multiple_gross IS NOT NULL AND exit_ts >= ? "
                "AND (notes IS NULL OR notes NOT LIKE 'scale_in_add%') "
                "ORDER BY exit_ts DESC LIMIT ?", (min_date, lookback))
        else:
            cur = db.execute(
                "SELECT r_multiple_gross FROM trades WHERE exit_ts IS NOT NULL "
                "AND r_multiple_gross IS NOT NULL "
                "AND (notes IS NULL OR notes NOT LIKE 'scale_in_add%') "
                "ORDER BY exit_ts DESC LIMIT ?",
                (lookback,))
        rs = [float(r[0]) for r in cur.fetchall()]
        db.close()
    except Exception:
        return 1.0, ""
    if len(rs) < min_trades:
        return 1.0, ""
    mean_r = sum(rs) / len(rs)
    if mean_r <= pause_thr:
        return 0.0, f"portfolio_regime_PAUSE(meanR={mean_r:+.2f}<={pause_thr}, n={len(rs)})"
    if mean_r <= throttle_thr:
        return throttle_scale, f"portfolio_regime_throttle(meanR={mean_r:+.2f}<={throttle_thr}, scale={throttle_scale}, n={len(rs)})"
    if mean_r >= boost_hi_thr:
        return boost_hi_scale, f"portfolio_regime_BOOST_HI(meanR={mean_r:+.2f}>={boost_hi_thr}, scale={boost_hi_scale}, n={len(rs)})"
    if mean_r >= boost_lo_thr:
        return boost_lo_scale, f"portfolio_regime_boost(meanR={mean_r:+.2f}>={boost_lo_thr}, scale={boost_lo_scale}, n={len(rs)})"
    return 1.0, ""


def _per_inst_dd_scale(cfg: dict, ins: str) -> float:
    """V_INST_DD live: halve risk on instruments whose cumulative R drawdown
    >= threshold_r. Computes from trade_journal r_multiple_gross history.

    Disabled unless cfg.per_inst_dd_throttle.enabled. Returns 1.0 (= no-op)
    when disabled, when journal missing, or when too few trades on instrument.

    Backtest validation (run_combo_optimization.py): combining V_NOV_dn @
    6.10% with this throttle (scale=0.5, threshold=5R) gives +67,872% /
    59.35% DD vs +65,907% / 59.92% DD without -- better return AND lower DD.
    """
    nb = cfg.get("per_inst_dd_throttle", {})
    if not nb.get("enabled", False):
        return 1.0
    import sqlite3
    threshold_r = float(nb.get("threshold_r", 5.0))
    scale = float(nb.get("scale", 0.5))
    try:
        db = sqlite3.connect("trade_journal.db")
        cur = db.execute(
            "SELECT r_multiple_gross FROM trades WHERE instrument=? "
            "AND exit_ts IS NOT NULL AND r_multiple_gross IS NOT NULL "
            "AND (notes IS NULL OR notes NOT LIKE 'scale_in_add%') "
            "ORDER BY exit_ts",
            (ins,),
        )
        rs = [float(r[0]) for r in cur.fetchall()]
        db.close()
    except Exception:
        return 1.0
    if len(rs) < 3:
        return 1.0
    cum = 0.0; peak = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
    dd_r = peak - cum
    return scale if dd_r >= threshold_r else 1.0


def _dd_circuit_breaker_scale(cfg: dict, state, eq: float) -> tuple[float, str]:
    """Account-level drawdown circuit breaker.

    Returns (scale, reason). Scale multiplies into the per-trade risk %:
      - dd_pct >= pause_pct  -> 0.0   (block entries)
      - dd_pct >= throttle_pct -> throttle_scale (e.g. 0.5)
      - otherwise              -> 1.0

    Drawdown computed off state.equity_peak vs current eq. No-op if
    cfg.dd_circuit_breaker.enabled is false or peak is unset.
    """
    cb = cfg.get("dd_circuit_breaker", {})
    if not cb.get("enabled", False):
        return 1.0, ""
    if state.equity_peak <= 0 or eq <= 0:
        return 1.0, ""
    dd_pct = max(0.0, (state.equity_peak - eq) / state.equity_peak * 100.0)
    pause_pct = float(cb.get("pause_pct", 60.0))
    throttle_pct = float(cb.get("throttle_pct", 40.0))
    throttle_scale = float(cb.get("throttle_scale", 0.5))
    if dd_pct >= pause_pct:
        return 0.0, f"dd_cb_pause(dd={dd_pct:.1f}%>={pause_pct}%)"
    if dd_pct >= throttle_pct:
        return throttle_scale, f"dd_cb_throttle(dd={dd_pct:.1f}%>={throttle_pct}%, scale={throttle_scale})"
    return 1.0, ""


def _profit_ratchet_scale(cfg: dict, state, eq: float) -> tuple[float, str]:
    """Profit ratchet: preserve banked gains so a bad month can't undo years.

    Mechanism: once eq exceeds start_equity * bank_above_mult, bank
    `bank_fraction` of the gains above the floor. Future risk computed off
    (eq - banked). Banked only grows -- never decreases.

    Returns (scale, reason). Scale = (eq - banked) / eq, clamped to
    [min_scale, 1.0]. No-op if cfg.profit_ratchet.enabled is false.

    Side effect: bumps state.banked_equity when threshold crossed.
    """
    pr = cfg.get("profit_ratchet", {})
    if not pr.get("enabled", False):
        return 1.0, ""
    if eq <= 0:
        return 1.0, ""
    # Initialize start_equity on first call
    if state.start_equity <= 0:
        state.start_equity = eq
    base = state.start_equity
    bank_above_mult = float(pr.get("bank_above_mult", 2.0))
    bank_fraction = float(pr.get("bank_fraction", 0.5))
    min_scale = float(pr.get("min_scale", 0.25))

    # Only bank when current eq exceeds floor + new gains relative to
    # already-banked level
    bank_floor = base * bank_above_mult
    if eq > bank_floor:
        # Gains above floor that haven't been banked yet
        unbanked_gain = (eq - bank_floor) - state.banked_equity
        if unbanked_gain > 0:
            to_bank = unbanked_gain * bank_fraction
            state.banked_equity += to_bank

    if state.banked_equity <= 0:
        return 1.0, ""
    scale = max(min_scale, (eq - state.banked_equity) / eq)
    return scale, f"ratchet(banked=${state.banked_equity:,.0f}, scale={scale:.2f})"


def _loss_streak_scale(cfg: dict, state) -> tuple[float, str]:
    """Consecutive-loss throttle. Halve (or scale) risk after N back-to-back
    losers. Resets the moment a winner closes.

    Faster reaction than the account-DD circuit breaker: a 5-loss streak at
    7% risk = -35% drawdown, but this throttle fires after just 3-4 losses,
    well before the 40% account-DD threshold.

    Returns (scale, reason). 1.0 / no-op when disabled or under threshold.
    """
    cfg_blk = cfg.get("loss_streak_throttle", {})
    if not cfg_blk.get("enabled", False):
        return 1.0, ""
    threshold = int(cfg_blk.get("threshold", 3))
    scale = float(cfg_blk.get("scale", 0.5))
    if state.consecutive_losses < threshold:
        return 1.0, ""
    return scale, f"loss_streak_throttle(consec={state.consecutive_losses}>={threshold}, scale={scale})"


def _per_instrument_risk_pct(cfg: dict, ins: str, default: float) -> float:
    """Per-instrument override of base_risk_pct. Set in
    cfg.tier_overrides[<ins>].risk_pct_override. Defaults to passed default."""
    overrides = cfg.get("tier_overrides", {}).get(ins, {})
    if isinstance(overrides, dict) and "risk_pct_override" in overrides:
        return float(overrides["risk_pct_override"])
    return default


def _emergency_mult_for(cfg: dict, ins: str) -> float:
    """Per-instrument override of delayed_sl.emergency_mult; falls back to global.

    Set in cfg.tier_overrides[<ins>].emergency_mult (e.g. 1.5 for tail-prone
    instruments like XAUUSD or ETHUSD). When absent, returns
    cfg.delayed_sl.emergency_mult (default 3.0).
    """
    overrides = cfg.get("tier_overrides", {}).get(ins, {})
    if isinstance(overrides, dict) and "emergency_mult" in overrides:
        return float(overrides["emergency_mult"])
    return float(cfg.get("delayed_sl", {}).get("emergency_mult", 3.0))


def warmup_models(cfg: dict, state: State) -> bool:
    rp = cfg["regime_predictor"]
    n = rp["warmup_bars"]
    print(f"[warmup] fetching {n} H1 bars per instrument...")

    ltf_by_name: dict[str, pd.DataFrame] = {}
    for ins in cfg["instruments"]:
        broker = cx.resolve_symbol(ins, cfg.get("symbol_map"))
        if broker is None:
            print(f"  [skip] {ins}: not available on this broker")
            continue
        state.broker_symbols[ins] = broker
        bars = cx.fetch_bars(broker, mt5.TIMEFRAME_H1, n)
        if len(bars) < rp["train_window"] + 200:
            print(f"  [skip] {ins} -> {broker}: only {len(bars)} bars (need >= {rp['train_window']+200})")
            continue
        ltf_by_name[ins] = bars
        print(f"  [ok]   {ins} -> {broker}: {len(bars)} H1 bars")

    if not ltf_by_name:
        print("[warmup] no instruments have enough data -- aborting")
        return False

    # Determine which lookups we need based on default tier + tier_overrides.
    # Some instruments may use ML, others may use current-bar ADX, others fixed.
    # We initialize both so per-instrument tiers work.
    needs_ml  = cfg["tier"].get("use_predicted_regime", False) or any(
        v.get("use_predicted_regime", False)
        for k, v in cfg.get("tier_overrides", {}).items()
        if not k.startswith("_")
    )
    needs_now = cfg["tier"].get("use_regime", False) or any(
        v.get("use_regime", False)
        for k, v in cfg.get("tier_overrides", {}).items()
        if not k.startswith("_")
    )

    if needs_ml:
        refit_hours = float(rp.get("refit_every_hours", 24.0))
        print(f"[warmup] initial ML regime predictor fit per instrument "
              f"(refit every {refit_hours}h or {rp['train_window']//4} new bars)")
        state.regime_manager = LiveRegimeManager(
            refit_every_hours=refit_hours,
            train_window=rp["train_window"],
            lookahead=rp["lookahead"],
            verbose=True,
        )
        state.regime_manager.force_refit_all(ltf_by_name)
        print(state.regime_manager.status().to_string(index=False))

    if needs_now:
        from regime import build_regime_lookup
        state.now_regime = build_regime_lookup(ltf_by_name)

    return True


def load_novelty_baselines(cfg: dict, state: State) -> None:
    """If novelty.enabled in config, load Mahalanobis baselines from disk.
    Sets state.novelty_baselines on success, None on failure (which makes
    novelty multiplier default to 1.0 = no scaling)."""
    nov_cfg = cfg.get("novelty", {})
    if not nov_cfg.get("enabled", False):
        return
    path = nov_cfg.get("baselines_path", "novelty_baselines.json")
    if not os.path.exists(path):
        print(f"[novelty] WARN: novelty.enabled=true but no baselines file at {path} -- multiplier defaults to 1.0")
        print(f"[novelty] run save_novelty_baselines.py to generate it")
        return
    try:
        state.novelty_baselines = load_baselines_from_disk(path)
        print(f"[novelty] loaded baselines for {len(state.novelty_baselines)} instruments from {path} "
              f"(mode={nov_cfg.get('mode', 'downscale_only')}, floor={nov_cfg.get('floor', 0.5)})")
    except Exception as e:
        print(f"[novelty] WARN: failed to load baselines from {path}: {e} -- multiplier defaults to 1.0")


def consider_instrument(ins: str, cfg: dict, state: State, cap_blocked: bool = False) -> None:
    """cap_blocked=True: the concurrency cap is full. We still evaluate the bar so
    the signal is JOURNALED (skip_reason='concurrency_cap') for counterfactuals
    -- before 2026-10-22 these signals vanished without a trace."""
    broker = state.broker_symbols.get(ins)
    if broker is None: return
    exe = cfg["execution"]

    # Already in a trade or with a pending order. We no longer return here
    # (2026-10-25 audit): the bar was left un-marked, so when the position
    # closed mid-bar the stale signal was taken minutes later at a chased
    # price (ETHUSD 2026-10-24: filled 1.7R from the signal close, RR 0.29).
    # Backtest semantics = in position at bar close means no entry on that
    # bar. So: evaluate the bar once, journal the signal as `in_position`
    # for counterfactuals, and burn the bar.
    in_pos = (cx.has_open_position(broker, exe["magic_number"])
              or cx.has_pending_order(broker, exe["magic_number"]))

    # Only act on a NEW closed H1 bar. NOTE: we do NOT yet mark this bar as
    # 'seen' -- that happens only after we've successfully evaluated it past
    # the spread/market-open gates. Otherwise a momentary spread spike at
    # bar close burns the bar forever (the cause of the 2026-05-08..13 drought).
    closed = cx.latest_closed_bar_time(broker, mt5.TIMEFRAME_H1)
    if closed is None: return
    # A bar that stayed spread-blocked until the next bar closed was never
    # evaluated: journal it now as spread_expired (2026-10-23).
    pend = _SPREAD_PENDING.get(ins)
    if pend is not None and pend[0] != closed:
        _SPREAD_PENDING.pop(ins, None)
        if pend[1] is not None and state.last_bar_seen.get(ins) != pend[0]:
            print(f"  [{ins}] bar {pend[0]} expired while spread-blocked (last spread {pend[6]:.1f}) -- journaled spread_expired")
            _log_signal(pend[1], ins, pend[2], float(pend[5]), pend[3], pend[4],
                         abs(pend[3] - pend[4]) / 2.5, "skipped", "spread_expired")
    if state.last_bar_seen.get(ins) == closed:
        return

    # Pull bars
    ltf = cx.fetch_bars(broker, mt5.TIMEFRAME_H1, 1500)
    htf = cx.fetch_bars(broker, mt5.TIMEFRAME_H4, 600)
    if len(ltf) < 250 or len(htf) < 60:
        return

    # Market-open check (skip silently if broker isn't actively quoting).
    # last_bar_seen NOT yet advanced -- will retry next poll.
    open_ok, open_reason = cx.is_market_open(broker)
    if not open_ok:
        return

    # 2026-10-24 gate order: signal first (cached per bar), then every cheap
    # gate (disabled / cap / blackout / news / session / hour / dow / auto-excl),
    # and the SPREAD gate LAST, just before sizing. Before, spread ran first and
    # a signal the session filter would have skipped looped thousands of times
    # (UK100 3,304 retries on one off-session bar) with no journal row.
    _tier_disabled = (cfg.get("tier_overrides", {}).get(ins, {}) or {}).get("enabled") is False
    info = mt5.symbol_info(broker); tick = mt5.symbol_info_tick(broker)
    if info is None or tick is None: return
    pip = info.point * (10 if info.digits in (3, 5) else 1)
    spread_pips = (tick.ask - tick.bid) / pip if pip > 0 else 999
    max_spread = exe["max_spread_pips_overrides"].get(ins, exe["max_spread_pips_default"])

    # Mark the bar as seen (evaluated). The spread gate below un-marks it when
    # it blocks, so the bar is retried on the next poll without re-running the
    # signal (cached) -- the 2026-05 "spread spike burns the bar" bug stays fixed.
    state.last_bar_seen[ins] = closed

    # Generate signal at the closed bar -- apply strategy_overrides from cfg.
    # Cached per (instrument, bar) so spread retries don't recompute it.
    cached = _SIG_CACHE.get(ins)
    if cached is not None and cached[0] == closed:
        row, sig_ts = cached[1], cached[2]
    else:
        so = cfg.get("strategy_overrides", {})
        if so:
            from dataclasses import replace as _replace
            params = _replace(
                DEFAULT_PARAMS,
                rr_target=float(so.get("rr_target", DEFAULT_PARAMS.rr_target)),
                atr_stop_mult=float(so.get("atr_stop_mult", DEFAULT_PARAMS.atr_stop_mult)),
            )
        else:
            params = DEFAULT_PARAMS
        sig = _get_entry_signal(cfg, ins, ltf, htf, params)
        # Last fully-closed bar in the signal frame = sig.iloc[-2] (sig.iloc[-1] = forming bar)
        if len(sig) < 2:
            _SIG_CACHE[ins] = (closed, None, None); return
        row, sig_ts = sig.iloc[-2], sig.index[-2]
        _SIG_CACHE[ins] = (closed, row, sig_ts)
    if row is None or pd.isna(row["side"]):
        _SPREAD_PENDING.pop(ins, None)
        return
    side = row["side"]
    sl   = float(row["sl"])
    tp   = float(row["tp"])
    sig_ts = sig.index[-2]
    # Compute cur_price ONCE here so every skip-path logger has it available.
    # (Was previously only set just before lot sizing, causing NameError when
    #  heat_cap / risk_zero / dd_circuit_breaker paths fired earlier.)
    cur_price = tick.ask if side == "long" else tick.bid

    if in_pos:
        _SPREAD_PENDING.pop(ins, None)
        print(f"  [{ins}] skip: signal while in position -- journaled in_position (bar burned)")
        _log_signal(sig_ts, ins, side, float(cur_price), float(sl), float(tp),
                     abs(cur_price - sl), "skipped", "in_position")
        return

    # Disabled instrument: journal as tier_disabled FIRST (moved up 2026-10-22 --
    # it used to sit after the hour/news/cap gates, so disabled instruments'
    # signals were being journaled as bad_hour_* and polluting those
    # counterfactual buckets with instruments that could never trade).
    if _tier_disabled:
        reason = (cfg.get("tier_overrides", {}).get(ins, {}) or {}).get("_disable_reason", "tier_overrides.enabled=false")
        print(f"  [{ins}] skip: disabled ({reason[:60]})")
        _log_signal(sig_ts, ins, side, float(cur_price), float(sl), float(tp),
                     abs(cur_price - sl), "skipped", "tier_disabled")
        return

    # Concurrency cap full: journal the signal as a skip (counterfactual data)
    # and burn the bar. Added 2026-10-22 after 7 signals were lost silently.
    if cap_blocked:
        print(f"  [{ins}] skip: concurrency cap full -- logged for counterfactual")
        _log_signal(sig_ts, ins, side, float(cur_price), sl, tp, abs(sl - tp) / 2.5,
                     "skipped", "concurrency_cap")
        return

    # Per-instrument hour-of-day blackout: skip new entries during configured
    # toxic UTC hours. Toggleable via cfg.entry_blackout.enabled. Default off.
    bb = cfg.get("entry_blackout", {})
    if bb.get("enabled", False):
        bb_hours = bb.get("per_instrument", {}).get(ins, [])
        if bb_hours and pd.Timestamp(sig_ts).hour in bb_hours:
            print(f"  [{ins}] skip: entry hour {pd.Timestamp(sig_ts).hour:02d}:00 UTC "
                  f"is in blackout {bb_hours}")
            _log_signal(sig_ts, ins, side, float(cur_price), sl, tp, abs(sl - tp) / 2.5,
                         "skipped", "blackout_hour")
            return

    # News-window blackout: skip entries within configured high-impact news
    # windows (NFP, FOMC, CPI, ECB, BOE) where spread spikes and erratic moves
    # break strategy assumptions. Uses CURRENT time (not signal bar time).
    # Each window can have an `applies_to` list -- only those instruments are affected.
    in_news, news_name = _in_news_blackout(cfg, datetime.now(tz=__import__("datetime").timezone.utc), ins=ins)
    if in_news:
        print(f"  [{ins}] skip: in {news_name} news-blackout window")
        _log_signal(sig_ts, ins, side, float(cur_price), sl, tp, abs(sl - tp) / 2.5,
                     "skipped", f"news_{news_name}")
        return

    # Per-asset session filter: skip entries outside the instrument's
    # configured cash-session hours (UTC). Mostly used for indices whose
    # off-hour spreads are too wide for clean fills.
    in_session_blackout, sess_name = _in_session_blackout(cfg, ins, sig_ts)
    if in_session_blackout:
        print(f"  [{ins}] skip: {sess_name} (signal at UTC hour outside session window)")
        _log_signal(sig_ts, ins, side, float(cur_price), sl, tp, abs(sl - tp) / 2.5,
                     "skipped", sess_name)
        return

    # Hour-of-day filter: block entries during historically negative-expectancy
    # UTC hours (Asian session / overnight thin-liquidity window).
    bad_hour, bh_reason = _in_bad_hour(cfg, sig_ts)
    if bad_hour:
        print(f"  [{ins}] skip: {bh_reason}")
        _log_signal(sig_ts, ins, side, float(cur_price), sl, tp, abs(sl - tp) / 2.5,
                     "skipped", bh_reason)
        return

    # Day-of-week filter: block entries on historically negative-expectancy days.
    bad_dow, bd_reason = _in_bad_dow(cfg, sig_ts)
    if bad_dow:
        print(f"  [{ins}] skip: {bd_reason}")
        _log_signal(sig_ts, ins, side, float(cur_price), sl, tp, abs(sl - tp) / 2.5,
                     "skipped", bd_reason)
        return

    # Auto-exclusion: block entries on instruments whose rolling live expectancy
    # has gone deeply negative AND which have been dormant >= min_dormant_days.
    # Catches the "strategy choice is wrong + signal rarely fires" pattern that
    # regime pulse surfaces daily but can't act on by itself.
    auto_excl, ae_reason = _should_auto_exclude(cfg, ins)
    if auto_excl:
        print(f"  [{ins}] skip: {ae_reason}")
        # journal a NORMALISED reason so counterfactuals aggregate (the detailed
        # string produced 13 distinct buckets for 16 skips -- audit 2026-10-24)
        _log_signal(sig_ts, ins, side, float(cur_price), sl, tp, abs(sl - tp) / 2.5,
                     "skipped", "auto_excl")
        return

    # SPREAD gate -- LAST, only for signals that passed every other gate.
    # Wide spread does not burn the bar: un-mark it and retry next poll
    # (signal is cached). If the block outlives the bar, the expiry check at
    # the top journals it as spread_expired.
    if spread_pips > max_spread:
        state.last_bar_seen[ins] = None
        if _SPREAD_PENDING.get(ins, (None,))[0] != closed:
            print(f"  [{ins}] skip-retry: spread {spread_pips:.2f} > max {max_spread} "
                  f"(bar {closed} will retry each poll; journaled if it expires)")
        _SPREAD_PENDING[ins] = (closed, sig_ts, side, float(sl), float(tp), float(cur_price), float(spread_pips))
        return
    _SPREAD_PENDING.pop(ins, None)

    # FILL SANITY (2026-10-25 audit): the signal's SL/TP are absolute levels
    # computed from the bar close. If the live price has already run away
    # from that close (late evaluation, long spread retry), the trade would
    # be entered with a shrunken reward-to-risk. Require the fill to sit
    # within max_drift_R of the signal close and the actual RR to stay above
    # min_rr; otherwise journal `stale_fill` and burn the bar.
    fs = cfg.get("execution", {}).get("fill_sanity", {})
    if fs.get("enabled", True):
        try:
            sig_close = float(ltf["close"].loc[sig_ts])
        except Exception:
            sig_close = float(cur_price)
        planned_R = abs(sig_close - sl)
        drift_R = abs(float(cur_price) - sig_close) / planned_R if planned_R > 0 else 0.0
        live_risk = abs(float(cur_price) - sl)
        rr_actual = abs(tp - float(cur_price)) / live_risk if live_risk > 0 else 0.0
        if drift_R > float(fs.get("max_drift_R", 0.3)) or rr_actual < float(fs.get("min_rr", 2.0)):
            print(f"  [{ins}] skip: stale fill -- price drifted {drift_R:.2f}R from signal close "
                  f"{sig_close:.5f} (now {cur_price:.5f}), actual RR {rr_actual:.2f} "
                  f"(limits drift<={fs.get('max_drift_R', 0.3)}R, RR>={fs.get('min_rr', 2.0)})")
            _log_signal(sig_ts, ins, side, float(cur_price), float(sl), float(tp),
                         abs(cur_price - sl), "skipped", "stale_fill")
            return

    # Resolve effective tier for THIS instrument: defaults override-merged with global
    overrides = cfg.get("tier_overrides", {}).get(ins, {})
    # (per-instrument disable flag is now checked right after signal
    #  generation, before the hour/news/cap gates -- see above, 2026-10-22)

    effective_tier = dict(cfg["tier"])
    for k, v in overrides.items():
        if not k.startswith("_"):
            effective_tier[k] = v

    # Regime + risk
    if effective_tier["use_predicted_regime"] and state.regime_manager is not None:
        # Live predictor takes the LATEST ltf so it always predicts on fresh features
        regime = state.regime_manager.predict(ins, ltf)
    elif effective_tier["use_regime"] and state.now_regime is not None:
        regime = state.now_regime(ins, sig_ts)
    else:
        regime = "neutral"

    eq, bal = cx.account_equity_and_balance()
    prev_peak = state.equity_peak
    state.equity_peak = max(state.equity_peak, eq)
    if state.equity_peak > prev_peak:
        state.save_runtime()  # persist new peak so DD-throttle survives restart

    # Novelty multiplier -- Mahalanobis distance of the most recent bar's
    # features from the per-instrument training centroid, mapped via
    # downscale_only or upscale_familiar to a risk multiplier in [floor, ceiling].
    # Defaults to 1.0 (no change) if novelty disabled or baselines missing.
    nov_mult = 1.0
    nov_cfg = cfg.get("novelty", {})
    if (nov_cfg.get("enabled", False)
            and state.novelty_baselines
            and ins in state.novelty_baselines):
        try:
            feats = build_features(ltf)
            if len(feats) > 0:
                nov_mult = float(novelty_multiplier_for_features(
                    state.novelty_baselines[ins],
                    feats.iloc[-1],
                    mode=nov_cfg.get("mode", "downscale_only"),
                    floor=float(nov_cfg.get("floor", 0.5)),
                    ceiling=float(nov_cfg.get("ceiling", 1.5)),
                ))
        except Exception as e:
            print(f"  [{ins}] novelty multiplier failed ({e}); defaulting to 1.0")
            nov_mult = 1.0

    # Per-instrument risk override (e.g. XAUUSD = 0.25%, ETHUSD = 0.4%) for
    # tail-prone instruments. Falls back to tier base_risk_pct if not set.
    per_ins_base = _per_instrument_risk_pct(cfg, ins, float(effective_tier["base_risk_pct"]))
    # V_INST_DD: halve risk on instruments currently in cumulative R drawdown
    # (default disabled; reads journal r_multiple_gross history).
    inst_dd_m = _per_inst_dd_scale(cfg, ins)
    if inst_dd_m < 1.0:
        print(f"  [{ins}] inst_dd_throttle active: scale={inst_dd_m}x")

    # Account-level DD circuit breaker (pause / throttle) and profit ratchet.
    dd_cb_scale, dd_cb_reason = _dd_circuit_breaker_scale(cfg, state, eq)
    if dd_cb_scale == 0.0:
        print(f"  [{ins}] skip: {dd_cb_reason}")
        _log_signal(sig_ts, ins, side, float(cur_price), float(sl), float(tp),
                     abs(cur_price - sl), "skipped", "dd_circuit_breaker")
        return
    if dd_cb_scale < 1.0:
        print(f"  [{ins}] {dd_cb_reason}")
    prev_banked = state.banked_equity
    prev_start = state.start_equity
    ratchet_scale, ratchet_reason = _profit_ratchet_scale(cfg, state, eq)
    if ratchet_scale < 1.0:
        print(f"  [{ins}] {ratchet_reason}")
    # If ratchet changed banked/start, persist immediately so a crash before
    # the next save doesn't reset the baseline.
    if state.banked_equity != prev_banked or state.start_equity != prev_start:
        state.save_runtime()

    # Loss-streak throttle: faster reaction than DD-circuit-breaker.
    streak_scale, streak_reason = _loss_streak_scale(cfg, state)
    if streak_scale < 1.0:
        print(f"  [{ins}] {streak_reason}")

    # Per-instrument expectancy throttle: react to a specific instrument's
    # regime shift (e.g., XAUUSD 1-for-6, DE40 0-for-4 in current journal).
    inst_exp_scale, inst_exp_reason = _inst_expectancy_scale(cfg, ins)
    if inst_exp_scale == 0.0:
        print(f"  [{ins}] skip: {inst_exp_reason}")
        _log_signal(sig_ts, ins, side, float(cur_price), float(sl), float(tp),
                     abs(cur_price - sl), "skipped", "inst_expectancy_pause")
        return
    if inst_exp_scale < 1.0:
        print(f"  [{ins}] {inst_exp_reason}")

    # Correlation-aware skip: don't stack same-direction bets on correlated
    # instruments (US indices, etc). Runs after throttles so we don't waste
    # cycles checking positions on trades that would be pause-blocked anyway.
    # Correlation-aware sizing: either skip (0.0) or scale (e.g. 0.5) based on
    # cfg.correlation_skip.mode. Skip preserves original behavior; scale lets
    # correlated trades happen at reduced position size.
    # Strong-regime filter: skip entries when H4 ADX indicates strong trend.
    # Backtest showed +0.02-0.04R meanR uplift at ~30% trade reduction.
    reg_skip, reg_reason = _strong_regime_skip(cfg, htf)
    if reg_skip:
        print(f"  [{ins}] skip: {reg_reason}")
        _log_signal(sig_ts, ins, side, float(cur_price), float(sl), float(tp),
                     abs(cur_price - sl), "skipped", "strong_regime_filter")
        return

    # ML-rescue filter: for instruments rescued from no-baseline-edge via ML,
    # require the model's predicted win-probability to exceed threshold.
    strat_name_for_ml = cfg.get("strategy_per_instrument", {}).get(ins, "pullback")
    if strat_name_for_ml.startswith("_"): strat_name_for_ml = "pullback"
    ml_skip, ml_reason = _ml_rescue_skip(cfg, ins, strat_name_for_ml, ltf, htf)
    if ml_skip:
        print(f"  [{ins}] skip: {ml_reason}")
        _log_signal(sig_ts, ins, side, float(cur_price), float(sl), float(tp),
                     abs(cur_price - sl), "skipped", "ml_rescue")
        return

    corr_scale, corr_reason = _correlation_scale(cfg, ins, side, cfg["execution"]["magic_number"])
    if corr_scale == 0.0:
        print(f"  [{ins}] skip: {corr_reason}")
        _log_signal(sig_ts, ins, side, float(cur_price), float(sl), float(tp),
                     abs(cur_price - sl), "skipped", "correlation_skip")
        return
    if corr_scale < 1.0:
        print(f"  [{ins}] {corr_reason}")

    # Portfolio-wide regime throttle: react to overall expectancy degradation.
    port_scale, port_reason = _portfolio_regime_scale(cfg)
    if port_scale == 0.0:
        print(f"  [{ins}] skip: {port_reason}")
        _log_signal(sig_ts, ins, side, float(cur_price), float(sl), float(tp),
                     abs(cur_price - sl), "skipped", "portfolio_regime_pause")
        return
    if port_scale < 1.0:
        print(f"  [{ins}] {port_reason}")

    risk_pct = dynamic_risk(
        per_ins_base, regime, eq, state.equity_peak,
        use_regime=effective_tier["use_regime"], use_equity=effective_tier["use_equity_throttle"],
        chop_skip=effective_tier["chop_skip"],
        novelty_multiplier=nov_mult * inst_dd_m * dd_cb_scale * ratchet_scale * streak_scale * inst_exp_scale * port_scale * corr_scale,
    )

    # Portfolio heat cap: total worst-case loss across all currently-open
    # positions plus this proposed trade must stay under cap.
    heat_cap = float(cfg.get("execution", {}).get("portfolio_heat_cap_pct", 0.0))
    if heat_cap > 0:
        cur_heat = _portfolio_open_risk_pct(cfg, exe["magic_number"], eq)
        if cur_heat + risk_pct > heat_cap:
            print(f"  [{ins}] skip: portfolio heat {cur_heat:.2f}% + this trade "
                  f"{risk_pct:.2f}% would exceed cap {heat_cap}%")
            _log_signal(sig_ts, ins, side, float(cur_price), float(sl), float(tp),
                         abs(cur_price - sl), "skipped", "heat_cap")
            return
    if risk_pct <= 0:
        print(f"  [{ins}] skip: risk=0 (regime={regime})")
        _log_signal(sig_ts, ins, side, float(cur_price), float(sl), float(tp),
                     abs(cur_price - sl), "skipped", "risk_zero")
        return

    # Lot sizing from SL distance (cur_price was computed earlier from same tick)
    sl_distance = abs(cur_price - sl)
    lot = cx.calc_lot(broker, risk_pct, sl_distance, equity=eq, side=side)
    if lot <= 0:
        print(f"  [{ins}] skip: lot=0 (sl_dist={sl_distance})")
        _log_signal(sig_ts, ins, side, float(cur_price), float(sl), float(tp),
                     abs(cur_price - sl), "skipped", "lot_zero")
        return

    # Sanity check: verify computed lot risks ~= target. If we land at
    # min_lot and that risks <50% of target, the trade is undersized
    # and we'd waste a signal. This was triggered by the JPN225/USA500
    # cross-currency tick_value bug -- leaving the check in as belt-and-suspenders.
    ot = mt5.ORDER_TYPE_BUY if side == "long" else mt5.ORDER_TYPE_SELL
    actual_loss_at_sl = mt5.order_calc_profit(ot, broker, lot, cur_price, sl)
    if actual_loss_at_sl is not None and actual_loss_at_sl < 0:
        actual_risk_pct = (-actual_loss_at_sl) / eq * 100

        # Compute WORST-CASE risk during the delay window (SL at disaster_sl).
        # By design the delayed_sl strategy accepts em*R exposure for delay_bars
        # hours to avoid intra-bar noise. We allow this up to max_worst_case_mult.
        dsl_cfg_peek = cfg.get("delayed_sl", {})
        use_delay_peek = bool(dsl_cfg_peek.get("enabled", False))
        delay_bars_peek = int(effective_tier.get("delay_sl_bars", dsl_cfg_peek.get("default_delay_bars", 0))) if use_delay_peek else 0
        worst_risk_pct = actual_risk_pct
        if use_delay_peek and delay_bars_peek > 0:
            em_peek = _emergency_mult_for(cfg, ins)
            disaster_sl_peek = cx.compute_disaster_sl(broker, side, cur_price, sl, em_peek)
            dl = mt5.order_calc_profit(ot, broker, lot, cur_price, disaster_sl_peek)
            if dl is not None and dl < 0:
                worst_risk_pct = (-dl) / eq * 100

        # 1) Undersize guard
        if actual_risk_pct < risk_pct * 0.5:
            print(f"  [{ins}] skip: actual risk {actual_risk_pct:.2f}% < 50% of target "
                  f"{risk_pct:.2f}% (lot={lot}, would risk ${-actual_loss_at_sl:.2f})")
            _log_signal(sig_ts, ins, side, float(cur_price), float(sl), float(tp),
                         abs(cur_price - sl), "skipped", "min_lot_undersize")
            return
        # 2) Planned-risk overshoot (broker min_lot or sizing math forcing planned >1.5x target)
        max_risk_mult = float(cfg.get("execution", {}).get("max_risk_overshoot_mult", 1.5))
        if actual_risk_pct > risk_pct * max_risk_mult:
            print(f"  [{ins}] skip: planned risk {actual_risk_pct:.2f}% > {max_risk_mult}x target "
                  f"{risk_pct:.2f}% (lot={lot}, would risk ${-actual_loss_at_sl:.2f}). "
                  f"Min_lot oversizing the trade.")
            _log_signal(sig_ts, ins, side, float(cur_price), float(sl), float(tp),
                         abs(cur_price - sl), "skipped", "risk_overshoot")
            return
        # 3) Worst-case during delay window (separate, looser threshold -- by design em*planned during delay)
        max_worst_case = float(cfg.get("execution", {}).get("max_worst_case_mult", 4.0))
        if worst_risk_pct > risk_pct * max_worst_case:
            print(f"  [{ins}] skip: worst-case (disaster-SL during delay) "
                  f"{worst_risk_pct:.2f}% > {max_worst_case}x target {risk_pct:.2f}% (lot={lot}, "
                  f"em={_emergency_mult_for(cfg, ins)}).")
            _log_signal(sig_ts, ins, side, float(cur_price), float(sl), float(tp),
                         abs(cur_price - sl), "skipped", "risk_overshoot_worst")
            return

    # Resolve delayed-SL config
    dsl_cfg = cfg.get("delayed_sl", {})
    use_delay = bool(dsl_cfg.get("enabled", False))
    delay_bars = int(effective_tier.get("delay_sl_bars", dsl_cfg.get("default_delay_bars", 0))) if use_delay else 0
    emergency_mult = _emergency_mult_for(cfg, ins)

    # Snapshot features at signal time for the journal (used by ML retrain)
    try:
        feat_row = build_features(ltf).iloc[-1].to_dict()
        # Strip NaN/non-serializable
        feat_row = {k: (float(v) if v == v else None) for k, v in feat_row.items()}
    except Exception:
        feat_row = {}

    planned_dist_val = float(abs(cur_price - sl))
    base_risk_val = float(effective_tier.get("base_risk_pct", risk_pct))

    if use_delay and delay_bars > 0:
        disaster_sl = cx.compute_disaster_sl(broker, side, cur_price, sl, emergency_mult)
        print(f"  [{ins}] {side.upper()} regime={regime} risk={risk_pct:.2f}% lot={lot} "
              f"entry~={cur_price:.5f} disaster_sl={disaster_sl:.5f} (planned_sl={sl:.5f}) "
              f"tp={tp:.5f}  delay={delay_bars}h")
        if cfg.get("dry_run"):
            return
        res = cx.place_market(broker, side, lot, disaster_sl, tp,
                               exe["magic_number"], exe["comment"])
        if res.ok:
            swap_at = int(time.time() + delay_bars * 3600)
            ticket = _resolve_just_opened_ticket(broker, exe["magic_number"]) or res.order
            state.pending_sl_swap[ticket] = {
                "planned_sl": float(sl), "tp": float(tp),
                "swap_at_unix": swap_at, "ins": ins, "broker": broker,
            }
            state.position_meta[ticket] = {
                "planned_dist": planned_dist_val,
                "entry_price": float(cur_price),
                "side": side, "ins": ins, "broker": broker,
            }
            state.save_runtime()
            try:
                journal.log_trade_open(
                    ticket=ticket, instrument=ins, broker_symbol=broker, side=side,
                    volume=lot, entry_ts=datetime.now(tz=__import__("datetime").timezone.utc),
                    entry_price=float(cur_price), sl_initial=float(disaster_sl),
                    sl_planned=float(sl), tp=float(tp), planned_dist=planned_dist_val,
                    regime_at_entry=regime, predicted_regime=regime,
                    features=feat_row, base_risk_pct=base_risk_val,
                    effective_risk_pct=float(risk_pct),
                    notes="delayed_sl",
                )
            except Exception as e:
                print(f"    [journal] log_trade_open failed: {e}")
            print(f"    OK ticket={ticket} deal={res.deal}  SL swap scheduled "
                  f"in {delay_bars}h ({datetime.utcfromtimestamp(swap_at):%Y-%m-%d %H:%M:%S} UTC)")
            _log_signal(sig_ts, ins, side, float(cur_price), float(sl), float(tp),
                         planned_dist_val, "taken", None, ticket=ticket)
        else:
            print(f"    FAIL retcode={res.retcode} ({res.description})")
            _log_signal(sig_ts, ins, side, float(cur_price), float(sl), float(tp),
                         planned_dist_val, "skipped", "place_order_fail")
    else:
        # Original path: place with planned SL immediately
        print(f"  [{ins}] {side.upper()} regime={regime} risk={risk_pct:.2f}% lot={lot} "
              f"entry~={cur_price:.5f} sl={sl:.5f} tp={tp:.5f}")
        if cfg.get("dry_run"):
            return
        res = cx.place_market(broker, side, lot, sl, tp, exe["magic_number"], exe["comment"])
        if res.ok:
            ticket = _resolve_just_opened_ticket(broker, exe["magic_number"]) or res.order
            state.position_meta[ticket] = {
                "planned_dist": planned_dist_val,
                "entry_price": float(cur_price),
                "side": side, "ins": ins, "broker": broker,
            }
            state.save_runtime()
            try:
                journal.log_trade_open(
                    ticket=ticket, instrument=ins, broker_symbol=broker, side=side,
                    volume=lot, entry_ts=datetime.now(tz=__import__("datetime").timezone.utc),
                    entry_price=float(cur_price), sl_initial=float(sl),
                    sl_planned=float(sl), tp=float(tp), planned_dist=planned_dist_val,
                    regime_at_entry=regime, predicted_regime=regime,
                    features=feat_row, base_risk_pct=base_risk_val,
                    effective_risk_pct=float(risk_pct),
                    notes="market",
                )
            except Exception as e:
                print(f"    [journal] log_trade_open failed: {e}")
            print(f"    OK deal={res.deal} order={res.order} ticket={ticket}")
            _log_signal(sig_ts, ins, side, float(cur_price), float(sl), float(tp),
                         planned_dist_val, "taken", None, ticket=ticket)
        else:
            print(f"    FAIL retcode={res.retcode} ({res.description})")
            _log_signal(sig_ts, ins, side, float(cur_price), float(sl), float(tp),
                         planned_dist_val, "skipped", "place_order_fail")


def _classify_outcome(close_deal, sl_planned, sl_at_exit, side, exit_price) -> str:
    """Classify how a trade ended.

    Note: MT5's Python API routes ALL EA-driven closes through
    DEAL_REASON_CLIENT (because they're API client requests, not server-side
    EA logic). So broker-side reason alone can't distinguish "EA close" from
    "human click in the UI". We fall back to the deal COMMENT we set in our
    own close_position_at_market calls to identify our system's closes.

    True manual closes (human in MT5 UI) have either empty comment or a
    default comment string from the terminal -- not our prefixes.
    """
    if close_deal is None:
        return "unknown"
    reason = getattr(close_deal, "reason", None)
    comment = (getattr(close_deal, "comment", "") or "").strip()

    # 1. Broker-side reasons (always authoritative)
    if reason == mt5.DEAL_REASON_SL: return "sl"
    if reason == mt5.DEAL_REASON_TP: return "tp"
    if reason == mt5.DEAL_REASON_SO: return "stopout"
    # NOTE 2026-10-21: DEAL_REASON_EXPERT check moved BELOW the comment-prefix
    # checks. On HFM every API close carries reason=EXPERT, so checking it here
    # labelled tp_safety / loss_cap / early_adverse_cut / stale_exit closes
    # all as "expert" and the per-handler attribution was lost.

    # 2. Our own programmatic closes (look at comment we set when calling
    #    cx.close_position_at_market). Each handler uses a distinct prefix.
    if comment == "tp_safety" or comment.startswith("tp_safety"):
        return "tp_safety"
    if "stale_exit" in comment:
        return "stale_exit"
    if "loss_cap" in comment:
        return "loss_cap"
    if "early_adverse_cut" in comment:
        return "early_adverse_cut"
    if comment.startswith("fx-edge"):
        # Generic fx-edge system close (catch-all for any future handler)
        return "system_close"
    if reason == mt5.DEAL_REASON_EXPERT:
        return "expert"   # API close with no recognised comment

    # 3. Broker-side automatic actions (futures rollover, symbol delisting, etc.)
    c_lower = comment.lower()
    if "expir" in c_lower or "rollover" in c_lower or "rollfwd" in c_lower:
        return "broker_rollover"
    if "split" in c_lower or "corporate" in c_lower:
        return "corporate_action"
    if reason == mt5.DEAL_REASON_ROLLOVER:
        return "broker_rollover"

    # 4. Truly manual (human in MT5 UI, or some path we didn't tag)
    return "manual"


def _postmortem_for_ticket(ticket: int, meta: dict, open_time_unix: float,
                           close_time_unix: float, cfg: dict) -> dict:
    """Compute MAE/MFE and exit_R under three alternative exit configs for a
    just-closed trade, from H1 bars. Pure function; never raises to caller.

    Added 2026-10-20 so exit-config questions (BE trigger, chandelier on/off)
    accumulate on every close instead of needing a replay script.
    """
    out: dict = {}
    try:
        side = (meta.get("side") or "").lower()
        e = float(meta.get("entry_price") or 0); pdist = float(meta.get("planned_dist") or 0)
        broker = meta.get("broker")
        if not (side and e and pdist and broker):
            return out
        rr = float(cfg.get("strategy_overrides", {}).get("rr_target", 2.5))
        sgn = 1 if side == "long" else -1
        tp = e + sgn * rr * pdist
        _tz = __import__("datetime").timezone.utc
        t0 = datetime.fromtimestamp(float(open_time_unix), tz=_tz) - timedelta(hours=30)
        t1 = datetime.fromtimestamp(float(close_time_unix), tz=_tz) + timedelta(hours=2)
        rates = mt5.copy_rates_range(broker, mt5.TIMEFRAME_H1, t0, t1)
        if rates is None or len(rates) < 20:
            return out
        df = pd.DataFrame(rates)
        tr = np.maximum(df.high - df.low, np.maximum((df.high - df.close.shift()).abs(),
                                                     (df.low - df.close.shift()).abs()))
        df["atr"] = tr.rolling(14).mean()
        seg = df[df.time >= float(open_time_unix)].reset_index(drop=True)
        if seg.empty:
            return out
        # MAE / MFE / bars_to_mfe
        if side == "long":
            adv = (e - seg.low).clip(lower=0); fav = (seg.high - e).clip(lower=0)
        else:
            adv = (seg.high - e).clip(lower=0); fav = (e - seg.low).clip(lower=0)
        out["mae_R"] = float(adv.max() / pdist)
        out["mfe_R"] = float(fav.max() / pdist)
        out["bars_to_mfe"] = int(fav.values.argmax()) + 1

        def replay(be_trig, be_off, ch_trig, ch_mult, use_ch):
            stop = e - sgn * pdist; mfe = 0.0; peak = e
            for _, b in seg.iterrows():
                hi, lo = float(b.high), float(b.low)
                atr = float(b.atr) if not np.isnan(b.atr) else pdist / 2
                if (side == "long" and lo <= stop) or (side == "short" and hi >= stop):
                    return sgn * (stop - e) / pdist
                if (side == "long" and hi >= tp) or (side == "short" and lo <= tp):
                    return sgn * (tp - e) / pdist
                f = (hi - e) if side == "long" else (e - lo)
                mfe = max(mfe, f / pdist)
                peak = max(peak, hi) if side == "long" else min(peak, lo)
                if mfe >= be_trig:
                    lock = e + sgn * be_off * pdist
                    stop = max(stop, lock) if side == "long" else min(stop, lock)
                if use_ch and mfe >= ch_trig:
                    trail = peak - sgn * ch_mult * atr
                    stop = max(stop, trail) if side == "long" else min(stop, trail)
            return sgn * (float(seg.close.iloc[-1]) - e) / pdist

        ch = cfg.get("chandelier", {})
        ct = float(ch.get("trigger_R", 2.0)); cm = float(ch.get("atr_mult", 2.5))
        out["alt_be10_R"] = float(replay(1.0, 0.5, ct, cm, True))
        out["alt_be15_R"] = float(replay(1.5, 0.5, ct, cm, True))
        out["alt_noch_R"] = float(replay(1.0, 0.5, ct, cm, False))
    except Exception:
        pass
    return out


def process_closed_trades(state: State, magic: int, cfg: dict | None = None) -> int:
    """Detect positions that have closed since last poll and log them to journal.
    Then drop their state entries."""
    open_tickets = {p.ticket for p in (mt5.positions_get() or []) if p.magic == magic}
    closed_tickets = [t for t in list(state.position_meta.keys()) if t not in open_tickets]
    if not closed_tickets:
        return 0

    n_logged = 0
    for ticket in closed_tickets:
        meta = state.position_meta.get(ticket, {})
        # Pull the close deal from MT5 history. The position has both an
        # opening DEAL_ENTRY_IN deal and a closing DEAL_ENTRY_OUT deal.
        try:
            deals = mt5.history_deals_get(position=ticket) or []
        except Exception:
            deals = []
        close_deal = None
        for d in deals:
            if d.entry == mt5.DEAL_ENTRY_OUT:
                close_deal = d
                break

        if close_deal is None:
            # Position closed but history not yet available -- try again on
            # the next poll. Don't drop state yet.
            continue

        try:
            outcome = _classify_outcome(
                close_deal, meta.get("planned_dist"),
                None, meta.get("side"), close_deal.price,
            )
            be_armed_flag    = ticket in state.be_armed
            delay_swap_fired = ticket not in state.pending_sl_swap and meta.get("planned_dist") is not None
            journal.log_trade_close(
                ticket=ticket,
                # deal.time is broker-time epoch; store real UTC like entry_ts (2026-10-22)
                exit_ts=float(close_deal.time) - cx._get_broker_offset(),
                exit_price=float(close_deal.price),
                outcome=outcome,
                profit_usd=float(close_deal.profit),
                commission_usd=float(getattr(close_deal, "commission", 0.0) or 0.0),
                swap_usd=float(getattr(close_deal, "swap", 0.0) or 0.0),
                sl_at_exit=None,
                be_armed=be_armed_flag,
                adaptive_tighten=False,  # tracked separately if needed
                delay_swap_fired=delay_swap_fired,
            )
            n_logged += 1
            # Per-trade post-mortem: MAE/MFE + exit_R under alternative exit
            # configs. Best-effort; failures are swallowed inside the helper.
            try:
                open_deal = next((d for d in deals if d.entry == mt5.DEAL_ENTRY_IN), None)
                if open_deal is not None and cfg is not None:
                    pm = _postmortem_for_ticket(ticket, meta, float(open_deal.time),
                                                float(close_deal.time), cfg)
                    if pm:
                        journal.update_postmortem(ticket, **pm)
                        print(f"  [postmortem] {meta.get('ins')} MAE={pm.get('mae_R',0):.2f}R "
                              f"MFE={pm.get('mfe_R',0):.2f}R  alt be1.0={pm.get('alt_be10_R',0):+.2f} "
                              f"be1.5={pm.get('alt_be15_R',0):+.2f} noch={pm.get('alt_noch_R',0):+.2f}")
            except Exception as _e:
                print(f"  [postmortem] skipped: {_e}")
            # Loss-streak counter: increment on losing close, reset on win.
            # Uses USD profit as the sign (commissions/swap can flip a tiny
            # R-positive into net negative; we want net outcome).
            # *** FIX 2026-06-05: only count true STRATEGY exits. Broker
            # rollovers, corporate actions, and human manual closes are not
            # the strategy's fault -- counting them would spuriously fire
            # the loss-streak throttle.
            STRATEGY_OUTCOMES = {"sl", "tp", "stopout", "expert", "tp_safety",
                                  "stale_exit", "loss_cap", "system_close",
                                  "early_adverse_cut"}
            net = float(close_deal.profit) + float(getattr(close_deal, "commission", 0.0) or 0.0) + float(getattr(close_deal, "swap", 0.0) or 0.0)
            if outcome in STRATEGY_OUTCOMES:
                if net < 0:
                    state.consecutive_losses += 1
                elif net > 0:
                    state.consecutive_losses = 0
            print(f"[journal] logged close: ticket={ticket} {meta.get('ins')} "
                  f"outcome={outcome} profit=${close_deal.profit:+.2f}  "
                  f"consec_losses={state.consecutive_losses}")
        except Exception as e:
            print(f"[journal] log_trade_close failed for ticket={ticket}: {e}")

        # Drop state for the closed ticket
        state.position_meta.pop(ticket, None)
        state.be_armed.discard(ticket)
        state.pending_sl_swap.pop(ticket, None)

    if n_logged:
        state.save_runtime()
    return n_logged


_CAP_BLOCK_LOGGED: int | None = None   # last position count at which the concurrency-cap block was logged
# Spread gate bookkeeping (2026-10-23): ins -> (closed_bar, sig_ts|None, side, sl, tp, price, spread_pips).
# Lets a bar that was spread-blocked until it expired be journaled as 'spread_expired'
# so the spread filter gets a counterfactual bucket like every other gate.
_SPREAD_PENDING: dict[str, tuple] = {}
# Per-bar signal cache: ins -> (closed_bar, signal_row|None, sig_ts|None). Spread
# retries re-run the cheap gates but never recompute the signal (2026-10-24).
_SIG_CACHE: dict[str, tuple] = {}
_STALE_POLLS: dict[int, int] = {}   # ticket -> consecutive polls seen not-open but not yet journaled
_STALE_DROP_AFTER = 10               # ~5 min at 30s polls


def _cleanup_closed_positions(state: State, magic: int) -> None:
    """Belt-and-suspenders: drop state for a position that is no longer open AND
    whose close could not be journaled for _STALE_DROP_AFTER consecutive polls
    (history never appeared). Gives process_closed_trades (which runs at the top
    of the NEXT poll) a real chance to log the close first.

    BUG FIX 2026-10-21: previously dropped on the FIRST poll a ticket was seen
    not-open. This runs at the END of the poll, right after the exit handlers
    close positions at market, so every programmatic close (tp_safety_net,
    loss_cap, early_adverse_cut, stale_exit) was removed from position_meta
    before process_closed_trades could see it. 17 closes (+18.5R / ~$711,
    12 winners) were silently lost since May; only broker-side SL/TP closes,
    which land between polls, were ever journaled.
    """
    open_tickets = {p.ticket for p in (mt5.positions_get() or []) if p.magic == magic}
    # reset counters for anything that is open again / was handled
    for t in list(_STALE_POLLS):
        if t in open_tickets or t not in state.position_meta:
            _STALE_POLLS.pop(t, None)
    to_drop = []
    for t in list(state.position_meta):
        if t in open_tickets:
            continue
        _STALE_POLLS[t] = _STALE_POLLS.get(t, 0) + 1
        if _STALE_POLLS[t] >= _STALE_DROP_AFTER:
            to_drop.append(t)
    stale_be = [t for t in state.be_armed if t not in open_tickets and t not in state.position_meta]
    for t in to_drop:
        print(f"[cleanup] ticket={t} not open for {_STALE_POLLS[t]} polls and never journaled -- dropping state")
        state.position_meta.pop(t, None); state.be_armed.discard(t); _STALE_POLLS.pop(t, None)
    for t in stale_be:
        state.be_armed.discard(t)
    if to_drop or stale_be:
        state.save_runtime()


def _reverse_logical_name(broker_sym: str, symbol_map: dict) -> str | None:
    """Map a broker symbol (e.g. 'XAUUSDr') back to the logical name ('XAUUSD')."""
    for ins, candidates in symbol_map.items():
        if isinstance(candidates, list) and broker_sym in candidates:
            return ins
        if candidates == broker_sym:
            return ins
    return None


def auto_adopt_orphan_positions(cfg: dict, state: State) -> int:
    """Scan open positions with our magic that aren't in position_meta.

    For each orphan:
      1. Infer planned SL distance = current_SL_distance / emergency_mult.
      2. Register in position_meta so BE trail and adaptive SL can manage it.
      3. Attempt to tighten SL to planned. If broker rejects (e.g. price has
         moved past planned SL), keep the wider current SL -- registration
         alone is enough for going-forward management.

    Idempotent: re-running skips already-tracked tickets.
    Returns number of orphans handled (registered, regardless of tighten outcome).
    """
    magic = cfg["execution"]["magic_number"]
    symbol_map = cfg.get("symbol_map", {})

    positions = mt5.positions_get() or []
    ours = [p for p in positions if p.magic == magic]
    orphans = [p for p in ours if int(p.ticket) not in state.position_meta]
    if not orphans:
        return 0

    print(f"[auto-adopt] found {len(orphans)} untracked open position(s) -- adopting")
    n_handled = 0

    for p in orphans:
        ticket = int(p.ticket)
        ins = _reverse_logical_name(p.symbol, symbol_map) or p.symbol
        side = "long" if p.type == mt5.POSITION_TYPE_BUY else "short"
        emergency_mult = _emergency_mult_for(cfg, ins)

        # Heuristic: if TP is closer than SL, the SL is likely at the wider
        # disaster level (em*planned), so divide it down. Otherwise SL is at
        # planned (or BE-tightened) and we use it directly.
        current_sl_dist = abs(p.price_open - p.sl)
        tp_dist = abs(p.tp - p.price_open) if p.tp else 0.0
        if tp_dist > 0 and current_sl_dist > 0 and (tp_dist / current_sl_dist) < 1.5:
            planned_dist = current_sl_dist / emergency_mult
        else:
            planned_dist = current_sl_dist
        if side == "long":
            planned_sl = p.price_open - planned_dist
        else:
            planned_sl = p.price_open + planned_dist

        # Register first -- always succeeds
        state.position_meta[ticket] = {
            "planned_dist": float(planned_dist),
            "entry_price":  float(p.price_open),
            "side": side, "ins": ins, "broker": p.symbol,
        }
        n_handled += 1

        # Best-effort journal entry so a future close has a row to update
        # (log_trade_open is idempotent -- skips if ticket already exists).
        try:
            # position.time is broker-time; journal stores UTC (2026-10-22 fix)
            entry_dt = datetime.fromtimestamp(float(p.time) - cx._get_broker_offset(), tz=__import__("datetime").timezone.utc)
            base_risk_val = float(cfg.get("tier", {}).get("base_risk_pct", 0.0))
            journal.log_trade_open(
                ticket=ticket, instrument=ins, broker_symbol=p.symbol, side=side,
                volume=float(p.volume), entry_ts=entry_dt,
                entry_price=float(p.price_open), sl_initial=float(p.sl),
                sl_planned=float(planned_sl), tp=float(p.tp or 0.0),
                planned_dist=float(planned_dist),
                regime_at_entry="adopted", predicted_regime="adopted",
                features={}, base_risk_pct=base_risk_val,
                effective_risk_pct=base_risk_val,
                notes="auto_adopted",
            )
        except Exception as e:
            print(f"  [{ins}] ticket={ticket}: journal insert failed: {e}")

        # Try to tighten SL. Skip silently if broker rejects (price may have
        # moved past the planned level -- keep the wider disaster SL alive).
        res = cx.modify_position_sl(ticket, planned_sl, p.tp, magic=magic)
        if res.ok:
            print(f"  [{ins}] ticket={ticket}: SL {p.sl:.5f} -> {planned_sl:.5f} (planned, tightened)")
        else:
            print(f"  [{ins}] ticket={ticket}: registered (SL kept at {p.sl:.5f}; "
                  f"tighten skipped: {res.description})")

    state.save_runtime()
    return n_handled


def _resolve_just_opened_ticket(broker: str, magic: int) -> int | None:
    """Pick the most-recent position with our magic on the given symbol.
    Used after a market send to grab the ticket for the position the deal opened."""
    pos = mt5.positions_get(symbol=broker)
    if not pos: return None
    ours = [p for p in pos if p.magic == magic]
    if not ours: return None
    # Most recent by position-open time (TradePosition has `time`, not `time_setup`)
    ours.sort(key=lambda p: p.time, reverse=True)
    return int(ours[0].ticket)


def process_scale_in(cfg: dict, state: State, magic: int) -> int:
    """Scale-in on winners. When an open position's MFE reaches trigger_R,
    place a second order (add_fraction × main volume) with SL at main's
    original entry price (BE on main) and TP at main's TP.

    Both legs then run INDEPENDENTLY. Main continues to its own SL/TP/BE-trail/
    chandelier. Add has its own SL (main_entry) and TP (main's TP). If main
    reverses to full -1R, add loses -0.5R (price retraced from +1R to -1R on
    main = -1R from add's +1R entry, scaled by add_fraction). If main reaches
    TP, add also reaches TP (same price), earning +1.5R × add_fraction extra
    on top of main's +2.5R.

    Sim 2026-10-07 on 62 recent trades: trigger_R=1.0, add_fraction=0.5
    yielded +0.053R expectancy uplift; trigger_R=0.5 yielded +0.115R.

    Marks main's meta.scale_in_fired=True so only one add per main. Add's
    meta carries is_scale_in=True so this handler doesn't scale-in on an add.

    No-op unless cfg.scale_in.enabled.
    """
    si = cfg.get("scale_in", {})
    if not si.get("enabled", False):
        return 0
    trigger_R = float(si.get("trigger_R", 1.0))
    add_fraction = float(si.get("add_fraction", 0.5))
    min_age_seconds = int(si.get("min_age_seconds", 60))
    per_ins = si.get("per_instrument", {}) or {}

    positions = mt5.positions_get() or []
    ours = [p for p in positions if p.magic == magic]
    if not ours:
        return 0
    now_unix = int(time.time())
    n_added = 0

    for p in ours:
        meta = state.position_meta.get(int(p.ticket), {})
        if meta.get("scale_in_fired"):
            continue
        if meta.get("is_scale_in"):
            continue  # don't add on an add
        planned_dist = meta.get("planned_dist")
        if not planned_dist or planned_dist <= 0:
            continue
        if _pos_age_seconds(p) < min_age_seconds:
            continue

        ins = meta.get("ins", p.symbol)
        over = per_ins.get(ins, {}) if isinstance(per_ins, dict) else {}
        trig_R = float(over.get("trigger_R", trigger_R))
        add_frac = float(over.get("add_fraction", add_fraction))

        side = "long" if p.type == mt5.POSITION_TYPE_BUY else "short"
        tick = mt5.symbol_info_tick(p.symbol)
        if tick is None:
            continue
        cur_price = float(tick.bid if side == "long" else tick.ask)
        if side == "long":
            mfe_R = (cur_price - float(p.price_open)) / planned_dist
        else:
            mfe_R = (float(p.price_open) - cur_price) / planned_dist
        if mfe_R < trig_R:
            continue

        broker = p.symbol
        info = mt5.symbol_info(broker)
        add_volume = round(float(p.volume) * add_frac, 2)
        # Snap to the symbol's volume_step (2026-10-24: ETHUSD step is 0.1, we
        # sent 0.25 and the broker rejected 'Invalid volume' 51 times in a row).
        if info is not None and float(info.volume_step) > 0:
            step = float(info.volume_step)
            add_volume = round(int(add_volume / step + 1e-9) * step, 8)
        # Give up after 3 failed attempts instead of retrying every poll forever.
        if int(meta.get("scale_in_attempts", 0)) >= 3:
            meta["scale_in_fired"] = True
            state.position_meta[int(p.ticket)] = meta
            state.save_runtime()
            print(f"[scale_in] {ins} main={p.ticket}: 3 failed attempts, giving up")
            continue
        if info is not None and add_volume < float(info.volume_min):
            print(f"[scale_in] {ins} main={p.ticket}: add_volume {add_volume} < "
                  f"min {info.volume_min}, skip")
            # Mark fired so we don't retry each poll
            meta["scale_in_fired"] = True
            state.position_meta[int(p.ticket)] = meta
            state.save_runtime()
            continue

        add_sl = float(p.price_open)   # BE on main's original entry
        add_tp = float(p.tp)

        print(f"[scale_in] {ins} main={p.ticket} MFE={mfe_R:.2f}R "
              f"-> add {add_volume} lots @ ~{cur_price:.5f} "
              f"SL={add_sl:.5f} TP={add_tp:.5f}")
        res = cx.place_market(broker, side, add_volume, add_sl, add_tp,
                              magic, "fx-edge scale_in")
        if not res.ok:
            desc = res.description if hasattr(res, "description") else res
            meta["scale_in_attempts"] = int(meta.get("scale_in_attempts", 0)) + 1
            state.position_meta[int(p.ticket)] = meta
            print(f"  [scale_in] {ins} add order FAILED ({meta['scale_in_attempts']}/3): {desc}")
            continue

        n_added += 1
        new_ticket = _resolve_just_opened_ticket(broker, magic) or res.order
        add_planned_dist = abs(cur_price - add_sl)
        state.position_meta[int(new_ticket)] = {
            "planned_dist": add_planned_dist,
            "entry_price": float(cur_price),
            "side": side, "ins": ins, "broker": broker,
            "is_scale_in": True,
            "parent_ticket": int(p.ticket),
        }
        meta["scale_in_fired"] = True
        state.position_meta[int(p.ticket)] = meta
        state.save_runtime()

        try:
            journal.log_trade_open(
                ticket=int(new_ticket), instrument=ins, broker_symbol=broker,
                side=side, volume=add_volume,
                entry_ts=datetime.now(tz=__import__("datetime").timezone.utc),
                entry_price=float(cur_price), sl_initial=float(add_sl),
                sl_planned=float(add_sl), tp=float(add_tp),
                planned_dist=add_planned_dist,
                regime_at_entry="scale_in", predicted_regime="scale_in",
                features={}, base_risk_pct=0.0,
                effective_risk_pct=0.0,
                notes=f"scale_in_add parent={p.ticket} mfe_trigger={mfe_R:.2f}R",
            )
        except Exception as e:
            print(f"  [scale_in] journal log failed: {e}")
        print(f"  [scale_in] OK ticket={new_ticket}")
    return n_added


def process_early_adverse_cut(cfg: dict, state: State, magic: int) -> int:
    """Early-adverse exit. Within the first `max_bars` H1 bars after entry,
    close any position whose max-favorable-excursion (MFE) has stayed below
    `mfe_floor_R` *and* whose max-adverse-excursion (MAE) has reached or
    exceeded `mae_trigger_R`. R is measured against the planned_dist captured
    at open (same unit as 1R stop distance).

    Rationale: pullback-trend / breakout entries that go straight into red
    with no favorable excursion are usually failed setups. Baseline exit
    stack (BE at +1R, chandelier at +2R, decay-SL on hours, position_loss_cap
    at 2.5R) has no mechanism targeting this specific pattern -- they all
    fire later. This fills that gap, catching ~0.5R losers before they
    extend to full -1R or gap-through-SL events.

    Protection: the MFE floor prevents cutting trades that showed any early
    favorable movement -- only pure "adverse from entry" patterns qualify.
    Per-instrument overrides via cfg.early_adverse_cut.per_instrument allow
    tighter settings on crypto (where this most hurts) vs. looser on FX.

    No-op unless cfg.early_adverse_cut.enabled. Skips very fresh positions
    (min_age_seconds) and positions beyond max_bars.
    """
    eac = cfg.get("early_adverse_cut", {})
    if not eac.get("enabled", False):
        return 0
    max_bars = int(eac.get("max_bars", 6))
    mae_trig_R_default = float(eac.get("mae_trigger_R", 0.5))
    mfe_floor_R_default = float(eac.get("mfe_floor_R", 0.25))
    min_age = int(eac.get("min_age_seconds", 60))
    per_ins = eac.get("per_instrument", {}) or {}

    positions = mt5.positions_get() or []
    ours = [p for p in positions if p.magic == magic]
    if not ours:
        return 0
    now_unix = int(time.time())
    n_closed = 0

    for p in ours:
        meta = state.position_meta.get(int(p.ticket), {})
        ins = meta.get("ins", p.symbol)
        planned_dist = meta.get("planned_dist")
        if not planned_dist or planned_dist <= 0:
            continue  # no 1R reference -> can't compute R

        # Per-instrument override for crypto / per-asset-class tuning
        over = per_ins.get(ins, {}) if isinstance(per_ins, dict) else {}
        mae_trig_R = float(over.get("mae_trigger_R", mae_trig_R_default))
        mfe_floor_R = float(over.get("mfe_floor_R", mfe_floor_R_default))
        max_bars_ins = int(over.get("max_bars", max_bars))

        # Fresh-position gate: avoid acting on opening-bar noise
        if _pos_age_seconds(p) >= 0 and _pos_age_seconds(p) < min_age:
            continue

        age_bars = _h1_bars_since(float(p.time), p.symbol)
        if age_bars > max_bars_ins:
            continue  # window closed, let normal exits take over

        side = "long" if p.type == mt5.POSITION_TYPE_BUY else "short"
        mae_pts, mfe_pts, _ = _h1_mae_mfe_since(
            float(p.time), float(p.price_open), side, p.symbol
        )
        mae_R = mae_pts / planned_dist
        mfe_R = mfe_pts / planned_dist

        if mfe_R < mfe_floor_R and mae_R >= mae_trig_R:
            print(f"[early_adverse_cut] {ins} ticket={p.ticket} side={side} "
                  f"age_bars={age_bars} MAE={mae_R:.2f}R MFE={mfe_R:.2f}R "
                  f"(trig MAE>={mae_trig_R:.2f}R, MFE<{mfe_floor_R:.2f}R, "
                  f"max_bars={max_bars_ins}) -- force close")
            res = cx.close_position_at_market(int(p.ticket), magic=magic,
                                              comment="fx-edge early_adverse_cut")
            if res.ok:
                n_closed += 1
                print(f"  [early_adverse_cut] {ins} ticket={p.ticket}: closed OK")
            else:
                desc = res.description if hasattr(res, "description") else res
                print(f"  [early_adverse_cut] {ins} ticket={p.ticket}: close failed: {desc}")
    return n_closed


def process_position_loss_cap(cfg: dict, state: State, magic: int) -> int:
    """Per-position max-loss cap. Force-close any open position whose
    current unrealized loss exceeds max_loss_pct_equity of current equity.

    Addresses tail-risk trades where the broker SL is wider than intended:
      - Auto-adopted positions (planned_dist heuristic underestimates risk)
      - Delay-window disaster SL (3R wide -> can hit -20%+ equity loss)
      - Wide swing-based SL on volatile instruments (e.g., XAUUSD)

    Real example that motivated this: a XAUUSD trade lost $-219 on a $674
    account = 32% of equity in a single position. A 10% cap would have
    closed it at -$67, saving $152.

    Runs BEFORE BE-trail / chandelier in the poll loop because those only
    tighten SL -- they can't help a position already deeply underwater.

    No-op unless cfg.position_loss_cap.enabled. Skips very fresh positions
    (min_age_seconds=60 default) to avoid firing on opening-bar volatility.
    """
    cap_cfg = cfg.get("position_loss_cap", {})
    if not cap_cfg.get("enabled", False):
        return 0
    max_pct = float(cap_cfg.get("max_loss_pct_equity", 10.0))
    min_age = int(cap_cfg.get("min_age_seconds", 60))

    positions = mt5.positions_get() or []
    ours = [p for p in positions if p.magic == magic]
    if not ours:
        return 0
    ai = mt5.account_info()
    if ai is None or ai.equity <= 0:
        return 0
    eq = float(ai.equity)
    now_unix = int(time.time())
    n_closed = 0

    for p in ours:
        # Broker-time safe age check: p.time can be in broker-local seconds
        # (ahead of UTC), which makes _pos_age_seconds(p) go NEGATIVE and
        # the gate always passes. Use bars-elapsed as the source of truth.
        age_bars = _h1_bars_since(float(p.time), p.symbol)
        if age_bars < 1 and _pos_age_seconds(p) >= 0 and _pos_age_seconds(p) < min_age:
            continue
        if p.profit >= 0:
            continue  # only losers
        loss_pct = (-p.profit) / eq * 100.0
        if loss_pct < max_pct:
            continue

        meta = state.position_meta.get(int(p.ticket), {})
        ins = meta.get("ins", p.symbol)
        print(f"[loss_cap] {ins} ticket={p.ticket}: unrealized loss "
              f"${p.profit:+.2f} = {loss_pct:.1f}% of equity ${eq:.2f} "
              f">= cap {max_pct}% -- force close")
        res = cx.close_position_at_market(int(p.ticket), magic=magic,
                                            comment="fx-edge loss_cap")
        if res.ok:
            n_closed += 1
            print(f"  [loss_cap] {ins} ticket={p.ticket}: closed OK")
        else:
            print(f"  [loss_cap] {ins} ticket={p.ticket}: close failed: "
                  f"{res.description if hasattr(res,'description') else res}")
    return n_closed


def process_tp_safety_net(cfg: dict, state: State, magic: int) -> int:
    """Backstop for missed broker TP triggers.

    For each open position with our magic, close at market if EITHER:
      (a) current bid (long) / ask (short) is within near_tp_buffer_R *
          planned_dist of the TP -- preemptive close to guarantee execution.
      (b) any of the last `recent_bars_to_check` H1 bars has high (long)
          or low (short) that touched or crossed the TP -- catches spikes
          the broker may have missed.

    Idempotent: if a position is already closed (TP fired normally), it
    won't be in positions_get() so nothing happens. Only fires once per
    position (close removes it from positions_get on next poll).
    """
    tns = cfg.get("tp_safety_net", {})
    if not tns.get("enabled", False):
        return 0
    buf_R = float(tns.get("near_tp_buffer_R", 0.0))
    bars_to_check = int(tns.get("recent_bars_to_check", 3))
    min_age = int(tns.get("min_age_seconds", 60))

    n_closed = 0
    positions = mt5.positions_get() or []
    ours = [p for p in positions if p.magic == magic]
    now_unix = int(time.time())

    for p in ours:
        # Skip positions with no TP set (e.g., auto-adopted with broker TP=0).
        # The near_tp math would be meaningless against a zero target.
        if not p.tp:
            continue
        # Don't close trades that just opened -- give them a moment to live.
        # Use bars-elapsed (broker-time safe) for age check; raw p.time can
        # be in broker-local seconds (often ahead of UTC) which makes
        # _pos_age_seconds(p) go NEGATIVE -- the gate would always pass.
        age_bars = _h1_bars_since(float(p.time), p.symbol)
        if age_bars < 1 and _pos_age_seconds(p) >= 0 and _pos_age_seconds(p) < min_age:
            continue

        meta = state.position_meta.get(int(p.ticket))
        planned_dist = (meta or {}).get("planned_dist") or abs(p.price_open - p.sl)
        if planned_dist <= 0:
            continue

        side = "long" if p.type == mt5.POSITION_TYPE_BUY else "short"
        tick = mt5.symbol_info_tick(p.symbol)
        if tick is None:
            continue
        cur_bid, cur_ask = tick.bid, tick.ask

        # (a) Current price within buffer of TP
        if side == "long":
            cur_dist_to_tp = p.tp - cur_bid
        else:
            cur_dist_to_tp = cur_ask - p.tp
        near_tp_threshold = buf_R * planned_dist
        near_tp = (cur_dist_to_tp <= near_tp_threshold and cur_dist_to_tp > -planned_dist)

        # (b) Recent bar touched/crossed TP
        bar_touched_tp = False
        rates = mt5.copy_rates_from_pos(p.symbol, mt5.TIMEFRAME_H1, 0, bars_to_check)
        if rates is not None and len(rates) > 0:
            for r in rates:
                if side == "long" and r["high"] >= p.tp:
                    bar_touched_tp = True; break
                if side == "short" and r["low"] <= p.tp:
                    bar_touched_tp = True; break

        if not (near_tp or bar_touched_tp):
            continue

        # Fire close
        ins = (meta or {}).get("ins") or _reverse_logical_name(p.symbol, cfg.get("symbol_map", {})) or p.symbol
        reason = []
        if near_tp:        reason.append(f"price within {buf_R}R of TP (cur dist {cur_dist_to_tp:.5f})")
        if bar_touched_tp: reason.append("recent bar touched TP")

        res = cx.close_position_at_market(int(p.ticket), magic=magic, comment="tp_safety")
        if res.ok:
            print(f"[tp-safety] {ins} ticket={p.ticket}: closed at market -- "
                  f"{' AND '.join(reason)}")
            n_closed += 1
        else:
            # Don't spam -- broker may be off-quote. Try again next poll.
            pass

    return n_closed


def process_be_trail(cfg: dict, state: State, magic: int) -> int:
    """Break-even trail: once running profit reaches `be_trigger_R * planned_dist`,
    move SL to `entry +/- be_offset_R * planned_dist`. Only tightens; arms once
    per position. Works in both delay phase and post-delay phase."""
    bt = cfg.get("be_trail", {})
    if not bt.get("enabled", False):
        return 0
    per_ins = bt.get("per_instrument", {})
    default_trigger = float(bt.get("default_trigger_R", 1.0))
    default_offset  = float(bt.get("default_offset_R", 0.0))

    n_modified = 0
    positions = mt5.positions_get() or []
    ours = [p for p in positions if p.magic == magic]

    for p in ours:
        if int(p.ticket) in state.be_armed:
            continue
        meta = state.position_meta.get(int(p.ticket))
        if meta is None:
            continue
        ins = meta.get("ins")
        cfg_ins = per_ins.get(ins, {})
        if not cfg_ins.get("active", True):
            continue

        trig = float(cfg_ins.get("be_trigger_R", default_trigger))
        off  = float(cfg_ins.get("be_offset_R",  default_offset))
        side = meta["side"]
        entry_price = float(meta["entry_price"])
        planned_dist = float(meta["planned_dist"])
        if planned_dist <= 0:
            continue

        tick = mt5.symbol_info_tick(p.symbol)
        if tick is None:
            continue
        cur_price = tick.bid if side == "long" else tick.ask

        # Profit in R units
        if side == "long":
            profit_R = (cur_price - entry_price) / planned_dist
            target_sl = entry_price + off * planned_dist
        else:
            profit_R = (entry_price - cur_price) / planned_dist
            target_sl = entry_price - off * planned_dist

        if profit_R < trig:
            continue

        # Only tighten -- refuse to widen the existing SL
        if side == "long" and target_sl <= p.sl:
            state.be_armed.add(int(p.ticket))   # arm anyway so we don't recheck
            continue
        if side == "short" and target_sl >= p.sl:
            state.be_armed.add(int(p.ticket))
            continue

        res = cx.modify_position_sl(int(p.ticket), target_sl, p.tp, magic=magic)
        if res.ok:
            state.be_armed.add(int(p.ticket))
            state.save_runtime()
            try:
                journal.update_trade_flag(int(p.ticket), be_armed=True)
            except Exception as e:
                print(f"  [journal] update_trade_flag failed: {e}")
            print(f"[BE-trail] {ins} ticket={p.ticket}: profit {profit_R:.2f}R >= {trig}R, "
                  f"SL {p.sl:.5f} -> {target_sl:.5f} (entry+{off}R)")
            n_modified += 1

    return n_modified


def process_stale_exit(cfg: dict, state: State, magic: int) -> int:
    """Force-close positions that have been open for more than stale_bars
    H1 bars without reaching stale_progress_R toward TP.

    Mirrors backtest_be_trail._walk_forward_be_trail's stale-exit branch:
    if (j - fill_pos) > stale_bars AND not progress_made AND not be_armed,
    the trade is closed at the next bar's open. Here we approximate by
    polling at the configured tick_interval and closing at market when:
      - position has been open > stale_bars hours, AND
      - position is NOT yet BE-armed (~= no real progress made), AND
      - current unrealized R is below progress_R.

    No-op unless cfg['stale_exit']['enabled'] is true.
    """
    se = cfg.get("stale_exit", {})
    if not se.get("enabled", False):
        return 0
    stale_bars = int(se.get("stale_bars", 72))
    progress_R = float(se.get("progress_R", 0.75))
    progress_target_R = progress_R

    n_closed = 0
    positions = mt5.positions_get() or []
    ours = [p for p in positions if p.magic == magic]
    now_unix = datetime.now(__import__("datetime").timezone.utc).timestamp()

    for p in ours:
        meta = state.position_meta.get(int(p.ticket))
        if meta is None:
            continue
        # Already BE-armed -> trade made progress; not stale by definition.
        if int(p.ticket) in state.be_armed:
            continue

        # Count ACTUAL H1 bars (not wall-clock hours) for backtest alignment.
        bars_open = _h1_bars_since(float(p.time), p.symbol)
        if bars_open <= stale_bars:
            continue

        side = meta.get("side")
        entry_price = float(meta.get("entry_price", 0.0))
        planned_dist = float(meta.get("planned_dist", 0.0))
        if planned_dist <= 0:
            continue

        tick = mt5.symbol_info_tick(p.symbol)
        if tick is None:
            continue
        cur_price = tick.bid if side == "long" else tick.ask
        if side == "long":
            unrealized_R = (cur_price - entry_price) / planned_dist
        else:
            unrealized_R = (entry_price - cur_price) / planned_dist

        # If we've reached progress_R, not stale; broker's BE-trail handler will eventually arm.
        if unrealized_R >= progress_target_R:
            continue

        ins = meta.get("ins", "?")
        print(f"[stale_exit] {ins} ticket={p.ticket}: bars_open={bars_open} > {stale_bars}, "
              f"unrealized={unrealized_R:.2f}R < {progress_target_R}R -> close at market")
        res = cx.close_position_at_market(int(p.ticket), magic=magic, comment="fx-edge stale_exit")
        if res.ok:
            n_closed += 1
            try:
                journal.update_trade_flag(int(p.ticket), stale_exit=True)
            except Exception as e:
                print(f"  [journal] stale_exit update failed: {e}")
        else:
            print(f"  [stale_exit] close failed: {res.error}")

    return n_closed


def process_adaptive_sl(cfg: dict, state: State, magic: int) -> int:
    """Regime-adaptive SL tightening for open positions.

    For each currently-open position with our magic:
      1. Look up the instrument and the per-instrument adaptive_sl config
      2. Skip if `active` is False or the position is still in its delay window
      3. Compute current regime via regime_manager (or current-bar ADX fallback)
      4. Increment chop streak on instrument; if streak >= dwell, compute target SL
      5. If target SL is TIGHTER than current SL, modify the position
    """
    asl = cfg.get("adaptive_sl", {})
    if not asl.get("enabled", False):
        return 0
    per_ins = asl.get("per_instrument", {})
    default_tighten = float(asl.get("default_tighten_atr", 0.5))
    default_dwell = int(asl.get("default_dwell", 2))

    n_modified = 0
    positions = mt5.positions_get() or []
    ours = [p for p in positions if p.magic == magic]

    # Reverse map broker symbol -> logical instrument
    rev = {b: ins for ins, b in state.broker_symbols.items()}

    now_unix = int(time.time())
    for p in ours:
        ins = rev.get(p.symbol)
        if ins is None: continue
        cfg_ins = per_ins.get(ins, {})
        if not cfg_ins.get("active", True):
            continue

        # Skip if the position is still inside its delay window -- let the
        # disaster SL handle that phase.
        meta = state.pending_sl_swap.get(int(p.ticket))
        if meta is not None and now_unix < meta["swap_at_unix"]:
            continue

        # Pull current bars + ATR
        ltf = cx.fetch_bars(p.symbol, mt5.TIMEFRAME_H1, 200)
        if len(ltf) < 30: continue
        from strategy import atr as _atr
        atr_now = float(_atr(ltf, 14).iloc[-1])
        close_now = float(ltf["close"].iloc[-1])
        if not np.isfinite(atr_now) or atr_now <= 0: continue

        # Current regime
        if state.regime_manager is not None:
            regime = state.regime_manager.predict(ins, ltf)
        else:
            from regime import compute_regime_series as _crs
            regime = _crs(ltf).iloc[-1]

        # Track chop streak per instrument in state (in-memory)
        streak = state.chop_streak.get(ins, 0)
        if regime == "chop":
            streak += 1
        else:
            streak = 0
        state.chop_streak[ins] = streak

        dwell = int(cfg_ins.get("dwell", default_dwell))
        if streak < dwell:
            continue

        tighten = float(cfg_ins.get("tighten_atr", default_tighten))
        side = "long" if p.type == mt5.POSITION_TYPE_BUY else "short"
        if side == "long":
            target_sl = close_now - tighten * atr_now
            # Only tighten (move SL up)
            if target_sl <= p.sl:
                continue
            # Don't put SL above current low to avoid instant trigger
            tick = mt5.symbol_info_tick(p.symbol)
            if tick is None or target_sl >= tick.bid: continue
        else:
            target_sl = close_now + tighten * atr_now
            if target_sl >= p.sl:
                continue
            tick = mt5.symbol_info_tick(p.symbol)
            if tick is None or target_sl <= tick.ask: continue

        res = cx.modify_position_sl(int(p.ticket), target_sl, p.tp, magic=magic)
        if res.ok:
            try:
                journal.update_trade_flag(int(p.ticket), adaptive_tighten=True)
            except Exception as e:
                print(f"  [journal] update_trade_flag failed: {e}")
            print(f"[adaptive-SL] {ins} ticket={p.ticket}: chop dwell={streak}, "
                  f"SL {p.sl:.5f} -> {target_sl:.5f}  (tighten {tighten} ATR)")
            n_modified += 1
        else:
            # Don't spam -- try again next poll
            pass

    return n_modified


def process_chandelier_sl(cfg: dict, state: State, magic: int) -> int:
    """Chandelier (ATR-trail) exit for big winners.

    Once a position has profit >= trigger_R, replace BE-lock with an
    ATR-trailing stop that lets the trade run far past the 2.5R TP.
    SL = close - atr_mult * ATR (long) / close + atr_mult * ATR (short).
    Only RAISES the SL (never lowers).

    This is the single biggest backtest improvement of 2026-05-19:
    +50.3M% vs +19.88M% baseline at LOWER DD (64.74% vs 69.20%).

    No-op unless cfg.chandelier.enabled is true. Skips positions still
    in delay window. Composes with BE-trail (BE-arm fires first at
    small profit; chandelier overtakes at the larger trigger).
    """
    ch = cfg.get("chandelier", {})
    if not ch.get("enabled", False):
        return 0
    trigger_R = float(ch.get("trigger_R", 2.0))
    atr_mult = float(ch.get("atr_mult", 2.0))
    atr_period = int(ch.get("atr_period", 14))

    n_modified = 0
    positions = mt5.positions_get() or []
    ours = [p for p in positions if p.magic == magic]
    now_unix = datetime.now(__import__("datetime").timezone.utc).timestamp()

    for p in ours:
        ticket = int(p.ticket)
        # Skip during delay window -- disaster SL governs
        swap_meta = state.pending_sl_swap.get(ticket)
        if swap_meta is not None and now_unix < swap_meta["swap_at_unix"]:
            continue
        meta = state.position_meta.get(ticket)
        if meta is None:
            continue
        side = meta.get("side")
        entry = float(meta.get("entry_price", 0.0))
        planned_dist = float(meta.get("planned_dist", 0.0))
        if planned_dist <= 0 or entry <= 0:
            continue

        tick = mt5.symbol_info_tick(p.symbol)
        if tick is None:
            continue
        cur = tick.bid if side == "long" else tick.ask
        if side == "long":
            profit_R = (cur - entry) / planned_dist
        else:
            profit_R = (entry - cur) / planned_dist
        if profit_R < trigger_R:
            continue

        # Pull bars to compute ATR
        bars = cx.fetch_bars(p.symbol, mt5.TIMEFRAME_H1, atr_period * 5 + 20)
        if len(bars) < atr_period + 5:
            continue
        from strategy import atr as _atr
        atr_now = float(_atr(bars, atr_period).iloc[-1])
        close_now = float(bars["close"].iloc[-1])
        if not np.isfinite(atr_now) or atr_now <= 0:
            continue

        if side == "long":
            candidate = close_now - atr_mult * atr_now
            if candidate <= p.sl:
                continue  # only raise
            tick2 = mt5.symbol_info_tick(p.symbol)
            if tick2 is None or candidate >= tick2.bid:
                continue
        else:
            candidate = close_now + atr_mult * atr_now
            if candidate >= p.sl:
                continue
            tick2 = mt5.symbol_info_tick(p.symbol)
            if tick2 is None or candidate <= tick2.ask:
                continue

        ins = meta.get("ins", "?")
        res = cx.modify_position_sl(ticket, candidate, p.tp, magic=magic)
        if res.ok:
            print(f"[chandelier] {ins} ticket={ticket}: profit={profit_R:.2f}R, "
                  f"SL {p.sl:.5f} -> {candidate:.5f} (close-{atr_mult}*ATR)")
            # Mark in be_armed so decay/stale skip this trade going forward.
            # CRITICAL: persist immediately so the chandelier-armed state survives
            # a crash/restart -- otherwise decay_sl would re-engage on a
            # chandelier-managed winner and fight it.
            state.be_armed.add(ticket)
            state.save_runtime()
            n_modified += 1

    return n_modified


def process_decay_sl(cfg: dict, state: State, magic: int) -> int:
    """Progressive time-decay SL tightening.

    After `start_bars` bars open, ratchet SL toward entry by `step_R *
    planned_dist` every `every_bars` bars, capped at `max_R * planned_dist`.
    Only tightens -- never widens. Skips positions still in delay window
    or already BE-armed (BE manages SL from there).

    Mirrors backtest_protection time-decay schedule. No-op unless
    cfg['decay_sl']['enabled'] is true.
    """
    ds = cfg.get("decay_sl", {})
    if not ds.get("enabled", False):
        return 0
    default_start = int(ds.get("start_bars", 6))
    default_every = max(int(ds.get("every_bars", 4)), 1)
    default_step = float(ds.get("step_R", 0.25))
    default_max = float(ds.get("max_R", 0.80))
    per_ins = ds.get("per_instrument", {})

    n_modified = 0
    positions = mt5.positions_get() or []
    ours = [p for p in positions if p.magic == magic]
    now_unix = datetime.now(__import__("datetime").timezone.utc).timestamp()

    for p in ours:
        ticket = int(p.ticket)
        # BE-armed -> BE governs SL; don't fight it
        if ticket in state.be_armed:
            continue
        # Still inside delay window -> disaster SL active
        swap_meta = state.pending_sl_swap.get(ticket)
        if swap_meta is not None and now_unix < swap_meta["swap_at_unix"]:
            continue

        meta = state.position_meta.get(ticket)
        if meta is None:
            continue
        side = meta.get("side")
        entry_price = float(meta.get("entry_price", 0.0))
        planned_dist = float(meta.get("planned_dist", 0.0))
        if planned_dist <= 0 or entry_price <= 0:
            continue

        ins = meta.get("ins", "?")
        # Per-instrument schedule override. If override has enabled=false or
        # start_bars=null/missing, skip decay for this instrument entirely.
        ins_cfg = per_ins.get(ins)
        if ins_cfg is not None:
            if ins_cfg.get("enabled", True) is False:
                continue
            if ins_cfg.get("start_bars") is None:
                continue
            start_bars = int(ins_cfg.get("start_bars", default_start))
            every_bars = max(int(ins_cfg.get("every_bars", default_every)), 1)
            step_R = float(ins_cfg.get("step_R", default_step))
            max_R = float(ins_cfg.get("max_R", default_max))
        else:
            start_bars = default_start
            every_bars = default_every
            step_R = default_step
            max_R = default_max

        # Count ACTUAL H1 bars (not wall-clock hours) to align with backtest.
        # Wall-clock hours overcount weekends/closures by ~5x for FX/indices.
        bars_elapsed = _h1_bars_since(float(p.time), p.symbol)
        if bars_elapsed < start_bars:
            continue

        # *** DATA-BACKED FIX (forensic agent, 2026-06-05) ***
        # If require_unfavorable=true, only decay-tighten trades that are
        # currently AT-OR-BELOW entry. Trades in profit are noise-tightened
        # too aggressively and get stopped at the decay-SL before reaching BE.
        # Agent found: 6 of 9 decay_tighten=1 & be_armed=0 trades hit
        # SL at -0.15R to -0.81R on otherwise-winnable setups. This gate
        # lets trades in profit develop further.
        require_unfavorable = bool(ds.get("require_unfavorable", True))
        tick = mt5.symbol_info_tick(p.symbol)
        if tick is None:
            continue
        cur_price = tick.bid if side == "long" else tick.ask
        if side == "long":
            current_R = (cur_price - entry_price) / planned_dist
        else:
            current_R = (entry_price - cur_price) / planned_dist
        if require_unfavorable and current_R > 0.0:
            # Trade is in profit -- give it room. BE-trail or chandelier will manage.
            continue

        steps_int = 1 + (bars_elapsed - start_bars) // every_bars
        tighten_R = min(steps_int * step_R, max_R)
        if side == "long":
            target_sl = entry_price - (1.0 - tighten_R) * planned_dist
            if target_sl <= p.sl:
                continue
            if target_sl >= tick.bid:
                continue
        else:
            target_sl = entry_price + (1.0 - tighten_R) * planned_dist
            if target_sl >= p.sl:
                continue
            if target_sl <= tick.ask:
                continue

        res = cx.modify_position_sl(ticket, target_sl, p.tp, magic=magic)
        if res.ok:
            try:
                journal.update_trade_flag(ticket, decay_tighten=True)
            except Exception:
                pass
            print(f"[decay-SL] {ins} ticket={ticket}: bars={bars_elapsed}, "
                  f"tighten={tighten_R:.2f}R  SL {p.sl:.5f} -> {target_sl:.5f}")
            n_modified += 1

    return n_modified


def process_pending_sl_swaps(state: State, magic: int) -> int:
    """For every pending swap whose timer has elapsed, modify the position's
    SL from disaster -> planned. Skips if existing SL is already TIGHTER than
    planned (e.g., BE trail already moved it past entry -- we never widen)."""
    if not state.pending_sl_swap: return 0
    now_unix = int(time.time())
    n_swapped = 0
    n_dropped = 0
    to_remove = []

    open_positions = {p.ticket: p for p in (mt5.positions_get() or []) if p.magic == magic}

    for ticket, meta in list(state.pending_sl_swap.items()):
        if ticket not in open_positions:
            to_remove.append(ticket)
            n_dropped += 1
            continue
        if now_unix < meta["swap_at_unix"]:
            continue
        p = open_positions[ticket]
        side = "long" if p.type == mt5.POSITION_TYPE_BUY else "short"
        # If existing SL is already tighter than planned (BE trail already moved
        # it), no swap needed -- just drop the pending entry.
        if (side == "long"  and p.sl >= meta["planned_sl"]) or \
           (side == "short" and p.sl <= meta["planned_sl"]):
            print(f"[swap-SL] {meta['ins']} ticket={ticket}: SL already at/past planned "
                  f"({p.sl:.5f} vs planned {meta['planned_sl']:.5f}) -- skip swap")
            to_remove.append(ticket)
            continue
        res = cx.modify_position_sl(ticket, meta["planned_sl"], meta["tp"], magic=magic)
        if res.ok:
            print(f"[swap-SL] {meta['ins']} ticket={ticket}: disaster SL -> planned SL "
                  f"({meta['planned_sl']:.5f}) OK")
            to_remove.append(ticket)
            n_swapped += 1
        else:
            if (now_unix - meta["swap_at_unix"]) % 600 < 30:
                print(f"[swap-SL] {meta['ins']} ticket={ticket}: modify failed "
                      f"retcode={res.retcode} ({res.description}) -- will retry")

    for t in to_remove:
        state.pending_sl_swap.pop(t, None)
    if to_remove:
        state.save_runtime()
    return n_swapped


def main_loop(cfg: dict, state: State):
    poll = cfg["polling"]["tick_interval_seconds"]
    hb_minutes = cfg["polling"]["log_heartbeat_minutes"]
    exe = cfg["execution"]
    magic = exe["magic_number"]

    # Concurrency cap
    max_concurrent = exe["max_concurrent_positions"]

    print(f"[main] entering loop (poll={poll}s)  Ctrl+C to stop")
    while True:
        try:
            # 0) Detect closes FIRST so we can log them with full context
            process_closed_trades(state, magic, cfg)   # cfg needed by the post-mortem (was NameError -> silently skipped, 2026-10-23)
            # 0a-pre) Periodic auto-adopt: pick up any untracked positions that
            #         appeared since the last poll (e.g., opened by a separate
            #         instance, recovered after crash, or manual orders with
            #         our magic). Without this, decay/chandelier/stale can't
            #         manage them. Runs every poll; idempotent (skips already-
            #         tracked tickets).
            auto_adopt_orphan_positions(cfg, state)
            # 0a-pre) Early-adverse cut -- within first N bars of a trade, if
            #     MFE never cleared mfe_floor_R and MAE reached mae_trigger_R,
            #     close at market. Catches "immediate opposite movement after
            #     entry" patterns before they develop into full -1R or
            #     gap-through-SL events. No-op if early_adverse_cut.enabled=false.
            process_early_adverse_cut(cfg, state, magic)
            # 0a) Per-position loss cap -- force-close any trade whose unrealized
            #     loss exceeds max_loss_pct_equity. Runs BEFORE other handlers
            #     so deeply-underwater positions are killed before BE/decay
            #     handlers try (and fail) to help.
            process_position_loss_cap(cfg, state, magic)
            # 0b) TP safety-net -- close near-TP positions before broker missed-trigger
            #     can turn a winner into a smaller winner / loser
            process_tp_safety_net(cfg, state, magic)
            # 1) BE trail first -- if a trade has reached profit trigger, lock BE.
            #    Run this before the swap so the swap can detect "already tighter".
            process_be_trail(cfg, state, magic)
            # 1b) Chandelier exit -- at +trigger_R profit, replace BE with
            #     ATR-trailing stop. Lets winners run far past TP.
            process_chandelier_sl(cfg, state, magic)
            # 1c) Scale-in on winners -- when main position's MFE reaches
            #     trigger_R, place an add order (add_fraction × main volume)
            #     with SL at main's entry (BE on main). Both legs run
            #     independently. No-op if scale_in.enabled=false.
            process_scale_in(cfg, state, magic)
            # 2) Pending SL swaps whose delay window has elapsed
            process_pending_sl_swaps(state, magic)
            # 3) Regime-adaptive SL tightening (no-op if adaptive_sl.enabled=false)
            process_adaptive_sl(cfg, state, magic)
            # 3b) Progressive time-decay SL -- institutional "breathe-then-squeeze"
            #     (no-op if decay_sl.enabled=false)
            process_decay_sl(cfg, state, magic)
            # 4) Stale-exit: close positions with no progress past stale_bars
            #    (no-op if stale_exit.enabled=false)
            process_stale_exit(cfg, state, magic)
            # Belt-and-suspenders cleanup if process_closed_trades couldn't fetch history
            _cleanup_closed_positions(state, magic)

            # Concurrency check (count our own positions).
            # 2026-10-22: scale-in adds are NOT independent positions (SL sits at
            # the main's entry, they live and die with the main) -- exclude them,
            # otherwise one winner with an add burns two of the slots. Also log
            # when the cap blocks: on 2026-10-22 it silently ate 7 valid signals.
            positions = mt5.positions_get() or []
            ours = [p for p in positions if p.magic == exe["magic_number"]
                    and not (getattr(p, "comment", "") or "").startswith("fx-edge scale_in")]
            slot_open = len(ours) < max_concurrent
            global _CAP_BLOCK_LOGGED
            if not slot_open and _CAP_BLOCK_LOGGED != len(ours):
                print(f"[main] concurrency cap: {len(ours)}/{max_concurrent} positions open -- new entries paused "
                      f"({', '.join(sorted(p.symbol for p in ours))})")
                _CAP_BLOCK_LOGGED = len(ours)
            elif slot_open and _CAP_BLOCK_LOGGED is not None:
                print(f"[main] concurrency cap cleared: {len(ours)}/{max_concurrent} positions open")
                _CAP_BLOCK_LOGGED = None

            for ins in cfg["instruments"]:
                # Cap full: still evaluate so the signal is journaled as a
                # 'concurrency_cap' skip (no order is placed in that mode).
                consider_instrument(ins, cfg, state, cap_blocked=not slot_open)

            # Heartbeat
            now = datetime.now(__import__("datetime").timezone.utc)
            if now - state.last_heartbeat >= timedelta(minutes=hb_minutes):
                eq, bal = cx.account_equity_and_balance()
                print(f"[heartbeat] {now:%H:%M:%S} equity=${eq:,.2f} balance=${bal:,.2f} "
                      f"open={len(ours)} peak=${state.equity_peak:,.2f}")
                state.last_heartbeat = now
        except Exception as e:
            print(f"[error] loop iteration: {e}")
        time.sleep(poll)


# ---- entry ---------------------------------------------------------------
class _TeeStream:
    """Duplicate stdout/stderr writes to a log file. Encoding: utf-8 with
    errors='replace' so non-ASCII chars never crash the loop again."""
    def __init__(self, *streams):
        self.streams = streams
    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass
    def flush(self):
        for s in self.streams:
            try: s.flush()
            except Exception: pass
    def isatty(self):
        return False


def _enable_log_tee():
    """Tee stdout + stderr to logs/live_trader_<timestamp>.log so a crashed
    or detached terminal never loses operational visibility again."""
    os.makedirs("logs", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join("logs", f"live_trader_{ts}.log")
    f = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = _TeeStream(sys.stdout, f)
    sys.stderr = _TeeStream(sys.stderr, f)
    print(f"[main] log tee active: {path}")
    return path


def _check_duplicate_instance() -> int:
    """Return PID of any other live_trader.py process. 0 if none.

    Prevents two instances from competing for the same magic positions.
    Tolerant of stale process listings during shutdown: a candidate PID
    must still be alive AND have a status that isn't 'zombie'/'dead'
    when re-probed. Otherwise we treat it as already-exited.
    """
    try:
        import psutil
        my_pid = os.getpid()
        my_parent = os.getppid()
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                pid = proc.info["pid"]
                if pid == my_pid or pid == my_parent: continue
                name = (proc.info["name"] or "").lower()
                if "python" not in name: continue
                cmd = " ".join(proc.info["cmdline"] or [])
                if "live_trader.py" not in cmd: continue
                # Re-probe: skip processes that are exiting/dead
                try:
                    status = proc.status()
                    if status in ("zombie", "dead", "stopped"): continue
                    # And confirm it's still alive RIGHT NOW
                    if not proc.is_running(): continue
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                return pid
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except ImportError:
        pass
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="mt5_runtime.json")
    ap.add_argument("--dry-run", action="store_true",
                     help="Compute signals + risk but do NOT send orders")
    ap.add_argument("--allow-duplicate", action="store_true",
                     help="Bypass duplicate-instance guard (debug only)")
    args = ap.parse_args()

    _enable_log_tee()

    other = _check_duplicate_instance()
    if other and not args.allow_duplicate:
        print(f"[fatal] another live_trader.py is already running (PID {other}). "
              f"Stop it first or use --allow-duplicate to bypass.")
        sys.exit(5)

    cfg = load_config(args.config)
    cfg["_config_path"] = args.config   # for the news_blackout hot-reload (runtime only, never written back)
    if args.dry_run:
        cfg["dry_run"] = True
        print("[main] DRY RUN -- no orders will be sent")

    conn = cfg.get("connection", {})
    print("[main] connecting to MT5 terminal ...")
    if not cx.connect(
        path=conn.get("path"),
        expected_server=conn.get("expected_server"),
        expected_login=conn.get("expected_login"),
    ):
        print("[fatal] MT5 connection failed or wrong terminal. "
              "Make sure the HFM terminal is open AND logged in, "
              "and the path/expected_server in mt5_runtime.json are correct.")
        sys.exit(2)

    ai = mt5.account_info()
    if ai is None:
        print("[fatal] could not read account info")
        sys.exit(3)
    print(f"[main] connected: account={ai.login} server={ai.server} "
          f"balance=${ai.balance:,.2f} equity=${ai.equity:,.2f}")

    state = State()
    state.equity_peak = float(ai.equity)
    state.load_runtime()
    if not warmup_models(cfg, state):
        sys.exit(4)
    load_novelty_baselines(cfg, state)

    # Self-heal: any open position with our magic that isn't tracked in
    # position_meta gets adopted at startup. Covers crashes, killed
    # processes, deleted state files, and trades opened by older versions.
    auto_adopt_orphan_positions(cfg, state)

    # Sanity-check restored pending swaps against currently open positions
    if state.pending_sl_swap:
        magic = cfg["execution"]["magic_number"]
        open_tickets = {p.ticket for p in (mt5.positions_get() or []) if p.magic == magic}
        stale = [t for t in state.pending_sl_swap if t not in open_tickets]
        for t in stale:
            state.pending_sl_swap.pop(t, None)
        if stale:
            print(f"[main] pruned {len(stale)} stale pending-swap entries")
        if state.pending_sl_swap:
            print(f"[main] {len(state.pending_sl_swap)} active pending SL swaps "
                  f"(swap times in UTC):")
            for t, m in state.pending_sl_swap.items():
                print(f"   ticket={t} ins={m['ins']} swap_at="
                      f"{datetime.utcfromtimestamp(m['swap_at_unix']):%Y-%m-%d %H:%M:%S}")
        state.save_runtime()

    def _shutdown(*_):
        print("\n[main] shutting down ...")
        cx.shutdown()
        sys.exit(0)
    os_signal.signal(os_signal.SIGINT, _shutdown)
    os_signal.signal(os_signal.SIGTERM, _shutdown)

    main_loop(cfg, state)


if __name__ == "__main__":
    main()
