"""Thin MetaTrader5 wrapper used by `live_trader.py`.

Responsibilities:
  - Connect / login (with retry)
  - Resolve a logical instrument name (e.g. "XAUUSD") to the broker's
    actual symbol (handles HFM suffixes via a mapping)
  - Pull H1 + H4 OHLC into pandas DataFrames in the same shape as the
    cached parquet files
  - Compute lot size from a risk % using the broker's tick value
  - Place orders with the IOC/FOK/RETURN fallback ladder we proved out
    earlier in PullbackTrendEA.mq5
  - Inspect open positions / pending orders for the EA's magic number
"""
from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Optional
import numpy as np
import pandas as pd

import MetaTrader5 as mt5


# ---------- connection ----------------------------------------------------
def connect(login: int | None = None, password: str | None = None,
            server: str | None = None, path: str | None = None,
            timeout: int = 60_000, retries: int = 3,
            expected_server: str | None = None,
            expected_login: int | None = None) -> bool:
    """Initialize the MT5 terminal connection.

    Safety guards:
      - `path`              : force-attach to a specific terminal64.exe (e.g. HFM's)
      - `expected_server`   : after attach, abort if account_info().server doesn't
                              CONTAIN this substring (case-insensitive)
      - `expected_login`    : after attach, abort if account_info().login differs

    Without `path`, mt5.initialize() picks any running terminal — order is
    undefined. If multiple brokers' MT5s are running on the same machine,
    you MUST pass `path` to lock the connection to the intended terminal.
    """
    for attempt in range(retries):
        kwargs = {"timeout": timeout}
        if path:     kwargs["path"] = path
        if login:    kwargs["login"] = login
        if password: kwargs["password"] = password
        if server:   kwargs["server"] = server
        ok = mt5.initialize(**kwargs)
        if ok:
            # Post-attach validation
            ai = mt5.account_info()
            if ai is None:
                print("[mt5] attached but account_info() empty")
                mt5.shutdown(); time.sleep(2); continue
            if expected_server and expected_server.lower() not in str(ai.server).lower():
                print(f"[mt5] WRONG SERVER: connected to '{ai.server}' but expected '{expected_server}' substring. Aborting.")
                mt5.shutdown()
                return False
            if expected_login and int(ai.login) != int(expected_login):
                print(f"[mt5] WRONG LOGIN: connected to {ai.login} but expected {expected_login}. Aborting.")
                mt5.shutdown()
                return False
            return True
        err = mt5.last_error()
        print(f"[mt5] init failed (attempt {attempt+1}/{retries}): {err}")
        time.sleep(2)
    return False


def shutdown():
    mt5.shutdown()


# ---------- symbol resolution --------------------------------------------
DEFAULT_SYMBOL_MAP = {
    # logical name -> candidate broker symbol patterns; we try in order
    "EURUSD": ["EURUSD", "EURUSD.r", "EURUSDm"],
    "GBPUSD": ["GBPUSD", "GBPUSD.r", "GBPUSDm"],
    "USDJPY": ["USDJPY", "USDJPY.r", "USDJPYm"],
    "CHFJPY": ["CHFJPY", "CHFJPY.r", "CHFJPYm"],
    "XAUUSD": ["XAUUSD", "GOLD",     "XAUUSD.r"],
    "US500":  ["US500",  "SPX500",   "USA500.cash", "SPX500.cash"],
    "US100":  ["US100",  "NAS100",   "USTEC.cash"],
    "US30":   ["US30",   "WS30",     "USA30.cash"],
    "DE40":   ["DE40",   "GER40",    "DAX40", "DE30"],
    "UK100":  ["UK100",  "FTSE100",  "UK100.cash"],
    "JP225":  ["JP225",  "JPN225",   "NIKKEI"],
    "HK50":   ["HK50",   "HSI",      "HK33"],
    "USOIL":  ["USOIL",  "WTI",      "CL.cash", "CRUDE.oil"],
    "ETHUSD": ["ETHUSD", "ETH/USD",  "ETHUSDm"],
    "BTCUSD": ["BTCUSD", "BTC/USD",  "BTCUSDm"],
}


def resolve_symbol(logical: str, custom_map: dict | None = None) -> str | None:
    """Return the broker's actual symbol name for a logical instrument.

    Prefers the first candidate that is actually TRADEABLE (trade_mode !=
    DISABLED). Falls back to the first candidate that merely exists, so
    data-only callers (history fetch) still resolve.

    BUG FIX 2026-10-20: HFM exposes both 'EURUSD' (quotes, bars, but
    trade_mode=DISABLED on this account) and 'EURUSDr' (tradeable). symbol_map
    listed the plain name first; this function returned it; consider_instrument
    then hit is_market_open()==False every poll and returned silently. Result:
    all 15 FX pairs (+US30/FRA40 for other reasons) produced ZERO live signals
    for months while the same code produced 20-146 signals/90d in backtest.
    """
    candidates = (custom_map or {}).get(logical) or DEFAULT_SYMBOL_MAP.get(logical, [logical])
    first_existing = None
    for s in candidates:
        info = mt5.symbol_info(s)
        if info is None:
            continue
        if first_existing is None:
            first_existing = s
        if getattr(info, "trade_mode", None) == mt5.SYMBOL_TRADE_MODE_DISABLED:
            continue
        if not info.visible:
            mt5.symbol_select(s, True)
        return s
    if first_existing is not None:
        info = mt5.symbol_info(first_existing)
        if info is not None and not info.visible:
            mt5.symbol_select(first_existing, True)
    return first_existing


# ---------- bar fetching --------------------------------------------------
# BUG FIX 2026-07-05: MT5 copy_rates_from_pos returns bar-open time as
# BROKER-LOCAL epoch seconds (HFM = GMT+2/+3), not real UTC. Labeling those
# seconds with utc=True kept them numerically the same but semantically wrong.
# Result: backtest H4 boundaries were 00/04/08/12/16/20 UTC, live H4 boundaries
# were broker-time (shifted 2-3h), so the trend filter EMAs read DIFFERENT bar
# aggregations. Detect broker offset once via tick vs real UTC and subtract
# before labeling. Everything downstream (session filters, news blackouts,
# H4 resampling) now sees real UTC.
_broker_offset_seconds: int | None = None


def _detect_broker_offset() -> int:
    """Detect broker server offset from real UTC in seconds.

    Uses a live tick from an FX symbol. During market closure the last tick
    is stale by hours or days, giving a bogus offset. In that case we round
    the raw tick offset to the nearest 3600s and only accept it if it lands
    in the plausible broker-offset window [-14h, +14h]. Otherwise fall back
    to an env override or the HFM default (GMT+3 = 10800s, GMT+2 winter).
    """
    v, _fresh = _detect_broker_offset_ex()
    return v


_HFM_OFFSETS = (7200, 10800)   # EET / EEST -- the only values HFM's server ever uses


def _eet_fallback_offset() -> int:
    """HFM server = Europe/EET: GMT+3 from the last Sunday of March 01:00 UTC
    to the last Sunday of October 01:00 UTC, else GMT+2. Exact rule (the old
    fallback used a rough Mar-30..Oct-26 window)."""
    import calendar
    from datetime import datetime, timezone as _tz
    now = datetime.now(_tz.utc)
    def last_sunday(y, m):
        d = max(wk[-1] for wk in calendar.monthcalendar(y, m)); return datetime(y, m, d, 1, 0, tzinfo=_tz.utc)
    return 10800 if last_sunday(now.year, 3) <= now < last_sunday(now.year, 10) else 7200


def _detect_broker_offset_ex() -> tuple[int, bool]:
    """Return (offset_seconds, fresh). fresh=True only when derived from a tick
    that is at most 5 minutes old; otherwise the EET calendar fallback.

    2026-10-24 rewrite (audit finding): the old version read the last FX tick
    and accepted anything within +-14h -- on a weekend the Friday-close tick
    made it return -5h, and the value was cached for the life of the process,
    so a weekend restart would have mislabelled every bar for a week. Now:
    24/7 crypto symbols first, accept only a tick that lands within 5 min of
    exactly +2h/+3h, and the caller re-detects hourly (DST flips are caught
    without a restart).
    """
    import os
    import time as _time
    env = os.environ.get("FXE_BROKER_OFFSET_SECONDS")
    if env is not None:
        try:
            return int(env), True
        except ValueError:
            pass
    for sym in ("#BTCUSDr", "#ETHUSDr", "BTCUSD", "ETHUSD", "EURUSDr", "XAUUSDr", "EURUSD", "XAUUSD", "USDJPYr"):
        try:
            if mt5.symbol_info(sym) is None:
                continue
            if not mt5.symbol_info(sym).visible:
                mt5.symbol_select(sym, True)
            tick = mt5.symbol_info_tick(sym)
            if not (tick and tick.time):
                continue
            raw = int(tick.time - _time.time())
            for cand in _HFM_OFFSETS:
                if abs(raw - cand) <= 300:          # fresh tick, exactly on an HFM offset
                    return cand, True
        except Exception:
            continue
    return _eet_fallback_offset(), False


_broker_offset_detected_at: float | None = None
_OFFSET_RECHECK_SECONDS = 3600


def _get_broker_offset() -> int:
    """Cached broker offset, re-validated hourly from a fresh tick. A stale-tick
    detection never overwrites a previously fresh value."""
    global _broker_offset_seconds, _broker_offset_detected_at
    import time as _time
    now = _time.time()
    if _broker_offset_seconds is None:
        v, fresh = _detect_broker_offset_ex()
        _broker_offset_seconds = v
        _broker_offset_detected_at = now if fresh else None
        print(f"[offset] broker offset {v/3600:+.0f}h ({'fresh tick' if fresh else 'EET calendar fallback'})")
    elif _broker_offset_detected_at is None or now - _broker_offset_detected_at >= _OFFSET_RECHECK_SECONDS:
        v, fresh = _detect_broker_offset_ex()
        if fresh:
            if v != _broker_offset_seconds:
                print(f"[offset] broker offset changed {_broker_offset_seconds/3600:+.0f}h -> {v/3600:+.0f}h (fresh tick)")
            _broker_offset_seconds = v
            _broker_offset_detected_at = now
        else:
            _broker_offset_detected_at = now - _OFFSET_RECHECK_SECONDS + 300   # retry in 5 min
    return _broker_offset_seconds


def _fetch_h1_raw(symbol: str, n_bars: int) -> pd.DataFrame:
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, n_bars)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"] - _get_broker_offset(), unit="s", utc=True)
    df = df.set_index("time")
    df = df.rename(columns={"tick_volume": "volume"})
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    return df[keep]


def fetch_bars(symbol: str, timeframe, n_bars: int = 5000) -> pd.DataFrame:
    # For H4, resample from H1 to match backtest boundaries (00/04/08/12/16/20 UTC).
    # Native MT5 H4 aggregates on broker-time boundaries which diverges from the
    # backtest's data.py:to_h4 origin="start_day" resample. That divergence was
    # the primary source of live/backtest expectancy gap identified 2026-07-05.
    if timeframe == mt5.TIMEFRAME_H4:
        h1 = _fetch_h1_raw(symbol, n_bars * 4 + 24)
        if h1.empty:
            return h1
        h4 = h1.resample("4h", origin="start_day", label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last",
             "volume": "sum" if "volume" in h1.columns else "last"}
        ).dropna(subset=["open", "high", "low", "close"])
        # Return the newest n_bars (backtest expects a rolling window)
        return h4.iloc[-n_bars:] if len(h4) > n_bars else h4
    # All other timeframes: raw fetch with corrected UTC labels
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n_bars)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"] - _get_broker_offset(), unit="s", utc=True)
    df = df.set_index("time")
    df = df.rename(columns={"tick_volume": "volume"})
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    return df[keep]


def latest_closed_bar_time(symbol: str, timeframe) -> pd.Timestamp | None:
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 1, 1)  # bar at index 1 = last closed
    if rates is None or len(rates) == 0:
        return None
    return pd.to_datetime(rates[0]["time"] - _get_broker_offset(), unit="s", utc=True)


def round_to_tick(price: float, info) -> float:
    """Snap a price to the symbol's trade_tick_size grid, then to its digits.

    2026-10-22: HFM's UK100 / FRA40 / USA30 / AUS200 report digits=2 but
    trade_tick_size=0.1. Rounding to digits alone produced off-grid stops
    (e.g. 10928.05) that the server rejects with 10016 'Invalid stops' --
    every UK100/FRA40 rejection since Oct 5 was off-grid, every fill on-grid.
    """
    try:
        tick = float(getattr(info, "trade_tick_size", 0) or 0)
        if tick <= 0:
            tick = float(info.point) if float(info.point) > 0 else 0.0
        if tick > 0:
            price = round(price / tick) * tick
        return round(price, info.digits)
    except Exception:
        return round(price, info.digits)


def is_market_open(symbol: str, max_tick_age_seconds: int = 90) -> tuple[bool, str]:
    """Best-effort 'is the market actively quoting?' check.

    Returns (open, reason). Avoids submitting orders when the broker is
    going to reject with `Market closed` — which spams the journal with
    failed-trade entries and makes real failures harder to spot.

    Heuristic:
      1. Symbol's trade_mode must allow full trading.
      2. Last tick must be < max_tick_age_seconds old.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        return False, "symbol_info missing"
    if info.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
        return False, "trade_mode disabled"
    if info.trade_mode == mt5.SYMBOL_TRADE_MODE_CLOSEONLY:
        return False, "trade_mode close-only"

    tick = mt5.symbol_info_tick(symbol)
    if tick is None or tick.time == 0:
        return False, "no tick"

    # tick.time is broker time: compare in the same base (2026-10-24; without
    # the offset the age was ~-3h all week and this guard could never fire).
    age = time.time() + _get_broker_offset() - tick.time
    if age > max_tick_age_seconds:
        return False, f"last tick {age:.0f}s old (>{max_tick_age_seconds})"
    if tick.bid == 0 or tick.ask == 0:
        return False, "bid/ask is zero"
    return True, "ok"


# ---------- position / order inspection ----------------------------------
def has_open_position(symbol: str, magic: int) -> bool:
    pos = mt5.positions_get(symbol=symbol)
    if not pos: return False
    return any(p.magic == magic for p in pos)


def has_pending_order(symbol: str, magic: int) -> bool:
    orders = mt5.orders_get(symbol=symbol)
    if not orders: return False
    return any(o.magic == magic for o in orders)


# ---------- lot sizing ----------------------------------------------------
def calc_lot(symbol: str, risk_pct: float, sl_distance_price: float,
              equity: float | None = None, side: str = "long") -> float:
    """Lot size such that loss at SL ~= risk_pct * equity, in ACCOUNT CURRENCY.

    Uses mt5.order_calc_profit() which delegates to the broker's profit
    calculation including cross-currency conversion. The previous
    implementation used info.trade_tick_value * (distance/tick_size), which
    silently broke for symbols denominated in non-account-currency (e.g.
    JPN225 quoted in JPY on a USD account — tick_value was returned in JPY
    and treated as USD, making loss-per-lot ~150x too large and the
    resulting lot clamped to min_lot).
    """
    if equity is None:
        ai = mt5.account_info()
        equity = ai.equity if ai else 0.0
    if equity <= 0 or sl_distance_price <= 0:
        return 0.0

    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        return 0.0
    min_lot, step, max_lot = info.volume_min, info.volume_step, info.volume_max
    risk_money = equity * risk_pct / 100.0

    # Use the broker's profit calculator at 1.0 lot to determine loss-per-lot.
    ref = tick.ask if side == "long" else tick.bid
    if side == "long":
        sl_price = ref - sl_distance_price
        order_type = mt5.ORDER_TYPE_BUY
    else:
        sl_price = ref + sl_distance_price
        order_type = mt5.ORDER_TYPE_SELL
    profit = mt5.order_calc_profit(order_type, symbol, 1.0, ref, sl_price)
    if profit is None or profit >= 0:
        # Either MT5 rejected the calc (e.g. closed market) or the math says
        # we'd profit at SL — both are nonsense here. Fall back to minimum lot
        # so the trade still attempts; the spread/stops checks elsewhere will
        # reject if the situation is genuinely degenerate.
        return min_lot
    loss_per_lot = -profit  # positive
    if loss_per_lot <= 0:
        return min_lot

    raw = risk_money / loss_per_lot
    lots = max(min_lot, min(max_lot, np.floor(raw / step) * step))
    return round(lots, 2)


# ---------- order placement ----------------------------------------------
FILLING_LADDER = [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]


@dataclass
class OrderResult:
    ok: bool
    retcode: int
    description: str
    deal: int = 0
    order: int = 0


def validate_stops(symbol: str, side: str, ref_price: float, sl: float, tp: float,
                   safety_margin_pts: int = 10) -> tuple[bool, str]:
    """Verify SL and TP are both at LEAST `stops_level + safety_margin` away
    from the reference price (current market for market orders, or the
    pending order's level for limits/stops). Returns (ok, reason).

    Order direction logic:
      LONG  -> SL below ref, TP above ref. Both distances must be >= min.
      SHORT -> SL above ref, TP below ref. Both distances must be >= min.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        return False, "symbol_info missing"
    point = info.point
    min_dist = (info.trade_stops_level + safety_margin_pts) * point

    if side == "long":
        if sl >= ref_price:
            return False, f"long SL {sl} >= ref {ref_price}"
        if tp <= ref_price:
            return False, f"long TP {tp} <= ref {ref_price}"
        if (ref_price - sl) < min_dist:
            return False, f"long SL too tight: {(ref_price - sl):.5f} < min {min_dist:.5f}"
        if (tp - ref_price) < min_dist:
            return False, f"long TP too tight: {(tp - ref_price):.5f} < min {min_dist:.5f}"
    else:
        if sl <= ref_price:
            return False, f"short SL {sl} <= ref {ref_price}"
        if tp >= ref_price:
            return False, f"short TP {tp} >= ref {ref_price}"
        if (sl - ref_price) < min_dist:
            return False, f"short SL too tight: {(sl - ref_price):.5f} < min {min_dist:.5f}"
        if (ref_price - tp) < min_dist:
            return False, f"short TP too tight: {(ref_price - tp):.5f} < min {min_dist:.5f}"
    return True, "ok"


def _send_with_filling_ladder(req: dict) -> OrderResult:
    last_retcode = 0
    last_comment = ""
    for fill in FILLING_LADDER:
        req2 = dict(req)
        req2["type_filling"] = fill
        res = mt5.order_send(req2)
        if res is None:
            err = mt5.last_error()
            last_retcode, last_comment = err[0] if isinstance(err, tuple) else -1, str(err)
            continue
        last_retcode = res.retcode
        last_comment = res.comment
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            return OrderResult(True, res.retcode, res.comment, res.deal, res.order)
        if res.retcode != mt5.TRADE_RETCODE_INVALID_FILL:
            return OrderResult(False, res.retcode, res.comment)
    return OrderResult(False, last_retcode, last_comment)


def place_market(symbol: str, side: str, lot: float, sl: float, tp: float,
                  magic: int, comment: str = "fx-edge") -> OrderResult:
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        return OrderResult(False, -1, "symbol_info/tick missing")
    price = tick.ask if side == "long" else tick.bid

    # Pre-flight: validate SL/TP distance vs HFM stops_level
    ok, reason = validate_stops(symbol, side, price, sl, tp)
    if not ok:
        return OrderResult(False, mt5.TRADE_RETCODE_INVALID_STOPS, f"stops invalid: {reason}")

    # Round SL/TP to the instrument's tick grid (not just digits -- see round_to_tick)
    sl = round_to_tick(sl, info)
    tp = round_to_tick(tp, info)

    req = {
        "action":   mt5.TRADE_ACTION_DEAL,
        "symbol":   symbol,
        "volume":   lot,
        "type":     mt5.ORDER_TYPE_BUY if side == "long" else mt5.ORDER_TYPE_SELL,
        "price":    price,
        "sl":       sl,
        "tp":       tp,
        "deviation":30,
        "magic":    magic,
        "comment":  comment,
    }
    return _send_with_filling_ladder(req)


def compute_disaster_sl(symbol: str, side: str, ref_price: float, planned_sl: float,
                         emergency_mult: float = 3.0) -> float:
    """Wider stop used during the delayed-SL phase.

    Distance from ref_price to the disaster SL = emergency_mult × distance
    from ref_price to planned_sl. So if the planned 1R is 100 pips, the
    disaster stop is at 3R.
    """
    if side == "long":
        planned_dist = abs(ref_price - planned_sl)
        return ref_price - emergency_mult * planned_dist
    else:
        planned_dist = abs(planned_sl - ref_price)
        return ref_price + emergency_mult * planned_dist


def close_position_at_market(ticket: int, magic: int = 0,
                              comment: str = "fx-edge close",
                              deviation_points: int = 30) -> OrderResult:
    """Force-close an open position with our magic at the current market price.
    Used by the TP safety-net when price has approached/crossed the TP level
    and we don't want to depend on the broker's TP trigger (which can be
    skipped during fast moves or off-hours)."""
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return OrderResult(False, -1, f"position {ticket} not found")
    p = positions[0]
    tick = mt5.symbol_info_tick(p.symbol)
    if tick is None:
        return OrderResult(False, -1, "tick missing")
    if p.type == mt5.POSITION_TYPE_BUY:
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
    else:
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask

    req = {
        "action":     mt5.TRADE_ACTION_DEAL,
        "symbol":     p.symbol,
        "volume":     p.volume,
        "type":       order_type,
        "position":   ticket,
        "price":      price,
        "deviation":  deviation_points,
        "magic":      magic,
        "comment":    comment,
    }
    return _send_with_filling_ladder(req)


def modify_position_sl(ticket: int, new_sl: float, new_tp: float | None = None,
                        magic: int = 0) -> OrderResult:
    """Update an open position's SL (and optionally TP). Used to swap the
    disaster SL for the planned SL after the delay window expires."""
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return OrderResult(False, -1, f"position {ticket} not found")
    p = positions[0]

    # Round to symbol precision
    info = mt5.symbol_info(p.symbol)
    if info is None:
        return OrderResult(False, -1, "symbol_info missing")
    new_sl = round_to_tick(new_sl, info)
    final_tp = round_to_tick(new_tp, info) if new_tp is not None else p.tp

    # Validate the new SL is on the right side of current price
    side = "long" if p.type == mt5.POSITION_TYPE_BUY else "short"
    tick = mt5.symbol_info_tick(p.symbol)
    if tick is None:
        return OrderResult(False, -1, "tick missing")
    ref = tick.bid if side == "long" else tick.ask
    ok, reason = validate_stops(p.symbol, side, ref, new_sl, final_tp)
    if not ok:
        return OrderResult(False, mt5.TRADE_RETCODE_INVALID_STOPS,
                           f"swap-SL invalid: {reason}")

    req = {
        "action":    mt5.TRADE_ACTION_SLTP,
        "position":  ticket,
        "symbol":    p.symbol,
        "sl":        new_sl,
        "tp":        final_tp,
        "magic":     magic,
    }
    res = mt5.order_send(req)
    if res is None:
        err = mt5.last_error()
        return OrderResult(False, -1, str(err))
    if res.retcode == mt5.TRADE_RETCODE_DONE:
        return OrderResult(True, res.retcode, res.comment, res.deal, res.order)
    return OrderResult(False, res.retcode, res.comment)


def place_pending_limit(symbol: str, side: str, lot: float, level: float,
                         sl: float, tp: float, magic: int,
                         expiry_seconds: int = 86400, comment: str = "fx-edge") -> OrderResult:
    """BUY/SELL LIMIT pending order. Falls back to market if level is inside the broker's freeze."""
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        return OrderResult(False, -1, "symbol_info/tick missing")

    stops_level_pts = info.trade_stops_level
    point = info.point
    freeze = max(stops_level_pts * point, point)
    bid, ask = tick.bid, tick.ask

    if side == "long":
        if level < ask - freeze:
            order_type = mt5.ORDER_TYPE_BUY_LIMIT
        elif level > ask + freeze:
            order_type = mt5.ORDER_TYPE_BUY_STOP
        else:
            return place_market(symbol, side, lot, sl, tp, magic, comment)
    else:
        if level > bid + freeze:
            order_type = mt5.ORDER_TYPE_SELL_LIMIT
        elif level < bid - freeze:
            order_type = mt5.ORDER_TYPE_SELL_STOP
        else:
            return place_market(symbol, side, lot, sl, tp, magic, comment)

    # Pre-flight: validate SL/TP distance vs the PENDING level (not market).
    # Broker checks stops relative to where the order will fill.
    ok, reason = validate_stops(symbol, side, level, sl, tp)
    if not ok:
        return OrderResult(False, mt5.TRADE_RETCODE_INVALID_STOPS, f"stops invalid: {reason}")

    sl = round_to_tick(sl, info)
    tp = round_to_tick(tp, info)
    level = round_to_tick(level, info)
    expiration = int(time.time()) + int(expiry_seconds)
    req = {
        "action":     mt5.TRADE_ACTION_PENDING,
        "symbol":     symbol,
        "volume":     lot,
        "type":       order_type,
        "price":      level,
        "sl":         sl,
        "tp":         tp,
        "type_time":  mt5.ORDER_TIME_SPECIFIED,
        "expiration": expiration,
        "magic":      magic,
        "comment":    comment,
    }
    return _send_with_filling_ladder(req)


# ---------- account / equity --------------------------------------------
def account_equity_and_balance() -> tuple[float, float]:
    ai = mt5.account_info()
    if ai is None: return 0.0, 0.0
    return float(ai.equity), float(ai.balance)
