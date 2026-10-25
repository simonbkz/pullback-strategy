"""Spread-aware backtester.

Generic across asset classes — pip size and typical spread come from the
Instrument record so FX, indices, commodities, and crypto all use the same
P&L accounting (pips / spread / R-multiples).
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from dataclasses import dataclass

from config import StrategyParams, Instrument
from strategy import generate_signals


@dataclass
class Trade:
    side: str
    entry_ts: pd.Timestamp
    entry_price: float
    sl: float
    tp: float
    exit_ts: pd.Timestamp
    exit_price: float
    outcome: str  # 'tp' / 'sl' / 'eod'
    risk_price: float
    pnl_price_gross: float
    pnl_pips_gross: float
    pnl_pips_net: float
    r_multiple_net: float


def apply_signal_filters(ltf: pd.DataFrame, sig: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    """Drop signals that fail session-time or volatility-regime gates.

    Session filter:    keeps only LTF bars within [session_start_utc, session_end_utc]
                       (yfinance indices are UTC).
    Vol-regime filter: keeps signals where ATR sits inside
                       [atr_norm_min, atr_norm_max] of its rolling mean.
    """
    side = sig["side"].copy()
    keep = side.notna()

    if p.session_filter:
        h = ltf.index.hour
        in_session = (h >= p.session_start_utc) & (h <= p.session_end_utc)
        keep = keep & pd.Series(in_session, index=ltf.index)

    if p.vol_regime_filter:
        a = sig["atr_at_signal"]
        roll = a.rolling(p.atr_norm_window, min_periods=50).mean()
        norm = a / roll
        keep = keep & ((norm >= p.atr_norm_min) & (norm <= p.atr_norm_max)).fillna(False)

    out = sig.copy()
    drop_mask = ~keep
    out.loc[drop_mask, "side"] = None
    out.loc[drop_mask, ["entry", "sl", "tp", "atr_at_signal"]] = np.nan
    return out


def _walk_forward(
    ltf: pd.DataFrame,
    sig: pd.DataFrame,
    spread_pips: float,
    pip: float,
    p: StrategyParams,
) -> list[Trade]:
    """Open a position on each non-null signal at next-bar open and walk bars
    until SL or TP hit (or max_bars_in_trade). If both touched on the same bar,
    SL takes precedence.

    With static stops (use_breakeven=False, use_trailing=False) we use a
    fast vectorized first-touch lookup. With dynamic stops we walk bar-by-bar
    so the running stop can be advanced (BE arm at N·R, then ATR trail).
    """
    trades: list[Trade] = []
    spread_price = spread_pips * pip
    dynamic = p.use_breakeven or p.use_trailing

    sides_full = sig["side"].values
    sig_mask = sig["side"].notna().values
    sig_positions = np.flatnonzero(sig_mask)
    if len(sig_positions) == 0:
        return trades

    highs   = ltf["high"].values.astype(np.float64)
    lows    = ltf["low"].values.astype(np.float64)
    closes  = ltf["close"].values.astype(np.float64)
    times   = ltf.index.values
    entries = sig["entry"].values.astype(np.float64)
    sls     = sig["sl"].values.astype(np.float64)
    tps     = sig["tp"].values.astype(np.float64)
    atrs    = sig["atr_at_signal"].values.astype(np.float64)
    n = len(ltf)
    open_until = -1
    max_bars = p.max_bars_in_trade

    for sig_pos in sig_positions:
        if sig_pos + 1 >= n:
            continue
        if sig_pos + 1 <= open_until:
            continue

        side = sides_full[sig_pos]
        entry_price = entries[sig_pos]
        sl0 = sls[sig_pos]
        tp = tps[sig_pos]
        atr_sig = atrs[sig_pos]
        if not (np.isfinite(entry_price) and np.isfinite(sl0) and np.isfinite(tp)):
            continue

        entry_eff = entry_price + 0.5 * spread_price if side == "long" else entry_price - 0.5 * spread_price
        risk_price = abs(entry_eff - sl0)
        if risk_price <= 0:
            continue

        start = sig_pos + 1
        end = min(start + max_bars, n)

        if not dynamic:
            h_slice = highs[start:end]
            l_slice = lows[start:end]
            if side == "long":
                sl_idx = np.where(l_slice <= sl0)[0]
                tp_idx = np.where(h_slice >= tp)[0]
            else:
                sl_idx = np.where(h_slice >= sl0)[0]
                tp_idx = np.where(l_slice <= tp)[0]
            sl_first = sl_idx[0] if sl_idx.size else -1
            tp_first = tp_idx[0] if tp_idx.size else -1

            if sl_first == -1 and tp_first == -1:
                outcome = "eod"
                exit_pos = end - 1
                exit_price = closes[end - 1]
            elif tp_first == -1 or (sl_first != -1 and sl_first <= tp_first):
                outcome = "sl"
                exit_pos = start + sl_first
                exit_price = sl0
            else:
                outcome = "tp"
                exit_pos = start + tp_first
                exit_price = tp
        else:
            sl_run = sl0
            be_armed = False
            outcome = "eod"
            exit_pos = end - 1
            exit_price = closes[end - 1]
            be_trigger = p.breakeven_at_r * risk_price
            trail_dist = p.trail_atr_mult * atr_sig if (p.use_trailing and np.isfinite(atr_sig)) else None

            if side == "long":
                for j in range(start, end):
                    hj, lj = highs[j], lows[j]
                    if lj <= sl_run:
                        outcome = "sl"; exit_pos = j; exit_price = sl_run; break
                    if hj >= tp:
                        outcome = "tp"; exit_pos = j; exit_price = tp; break
                    if p.use_breakeven and not be_armed and hj >= entry_eff + be_trigger:
                        if entry_eff > sl_run:
                            sl_run = entry_eff
                        be_armed = True
                    if trail_dist is not None and be_armed:
                        cand = hj - trail_dist
                        if cand > sl_run:
                            sl_run = cand
            else:
                for j in range(start, end):
                    hj, lj = highs[j], lows[j]
                    if hj >= sl_run:
                        outcome = "sl"; exit_pos = j; exit_price = sl_run; break
                    if lj <= tp:
                        outcome = "tp"; exit_pos = j; exit_price = tp; break
                    if p.use_breakeven and not be_armed and lj <= entry_eff - be_trigger:
                        if entry_eff < sl_run:
                            sl_run = entry_eff
                        be_armed = True
                    if trail_dist is not None and be_armed:
                        cand = lj + trail_dist
                        if cand < sl_run:
                            sl_run = cand

        exit_eff = exit_price - 0.5 * spread_price if side == "long" else exit_price + 0.5 * spread_price
        if side == "long":
            pnl_price_gross = exit_price - entry_price
            pnl_price_net   = exit_eff   - entry_eff
        else:
            pnl_price_gross = entry_price - exit_price
            pnl_price_net   = entry_eff   - exit_eff

        risk_pips = risk_price / pip
        trades.append(Trade(
            side=side,
            entry_ts=times[start],
            entry_price=float(entry_price),
            sl=float(sl0), tp=float(tp),
            exit_ts=times[exit_pos],
            exit_price=float(exit_price),
            outcome=outcome,
            risk_price=float(risk_price),
            pnl_price_gross=float(pnl_price_gross),
            pnl_pips_gross=float(pnl_price_gross / pip),
            pnl_pips_net=float(pnl_price_net / pip),
            r_multiple_net=float((pnl_price_net / pip) / risk_pips) if risk_pips > 0 else 0.0,
        ))
        open_until = exit_pos

    return trades


def equity_curve(trades: list[Trade], risk_pct: float, start_equity: float = 10_000.0) -> pd.Series:
    eq = start_equity
    pts = []
    for t in trades:
        pnl = eq * (risk_pct / 100.0) * t.r_multiple_net
        eq += pnl
        pts.append((t.exit_ts, eq))
    if not pts:
        return pd.Series(dtype=float)
    return pd.Series(dict(pts)).sort_index()


def max_drawdown_pct(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = (peak - equity) / peak
    return float(dd.max() * 100)


def summarize(ins: Instrument, trades: list[Trade], risk_pct: float) -> dict:
    if not trades:
        return {
            "name": ins.name, "asset_class": ins.asset_class, "trades": 0,
            "win_rate": 0.0, "profit_factor": 0.0,
            "expectancy_R_net": 0.0, "expectancy_pips_net": 0.0,
            "gross_pips": 0.0, "net_pips": 0.0,
            "spread_cost_pips": 0.0, "spread_pips_assumed": ins.spread_pips,
            "edge_over_spread": 0.0,
            "total_return_pct": 0.0, "max_dd_pct": 0.0,
        }
    df = pd.DataFrame([t.__dict__ for t in trades])
    wins = df[df["pnl_pips_net"] > 0]
    losses = df[df["pnl_pips_net"] <= 0]
    gp = wins["pnl_pips_net"].sum() if not wins.empty else 0.0
    gl = -losses["pnl_pips_net"].sum() if not losses.empty else 0.0
    pf = gp / gl if gl > 0 else (np.inf if gp > 0 else 0.0)
    spread_cost = float(df["pnl_pips_gross"].sum() - df["pnl_pips_net"].sum())
    avg_net = float(df["pnl_pips_net"].mean())

    eq = equity_curve(trades, risk_pct)
    total_return = (eq.iloc[-1] / 10_000.0 - 1) * 100 if not eq.empty else 0.0

    return {
        "name": ins.name,
        "asset_class": ins.asset_class,
        "trades": int(len(df)),
        "win_rate": float((df["pnl_pips_net"] > 0).mean() * 100),
        "profit_factor": float(pf) if np.isfinite(pf) else 999.0,
        "expectancy_R_net": float(df["r_multiple_net"].mean()),
        "expectancy_pips_net": avg_net,
        "gross_pips": float(df["pnl_pips_gross"].sum()),
        "net_pips": float(df["pnl_pips_net"].sum()),
        "spread_cost_pips": spread_cost,
        "spread_pips_assumed": ins.spread_pips,
        "edge_over_spread": float(avg_net / ins.spread_pips),
        "total_return_pct": float(total_return),
        "max_dd_pct": max_drawdown_pct(eq),
    }


def backtest_instrument(
    ins: Instrument,
    ltf: pd.DataFrame,
    htf: pd.DataFrame,
    params: StrategyParams,
) -> tuple[dict, list[Trade]]:
    sig = generate_signals(ltf, htf, params)
    sig = apply_signal_filters(ltf, sig, params)
    trades = _walk_forward(ltf, sig, spread_pips=ins.spread_pips, pip=ins.pip_size, p=params)
    stats = summarize(ins, trades, params.risk_pct)
    return stats, trades
