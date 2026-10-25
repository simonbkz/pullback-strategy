"""For each ship-grade instrument on the HFM account, report:
  - stops_level (min distance for SL/TP from price, in points)
  - freeze_level (distance within which orders can't be modified)
  - point size and pip size
  - typical SL distance the strategy actually computes (vs the minimum)
  - whether the strategy SL falls inside or outside the broker's required minimum
"""
import json
import pandas as pd
import MetaTrader5 as mt5

import live_mt5_connector as cx
from config import DEFAULT_PARAMS
from strategy import generate_signals

with open("mt5_runtime.json", encoding="utf-8") as f:
    cfg = json.load(f)

if not cx.connect():
    raise SystemExit(f"connect failed: {mt5.last_error()}")

print(f"{'instrument':>10} {'broker':>10} {'stops':>6} {'freeze':>6} {'point':>10} "
      f"{'pip':>10} {'min_sl_dist_price':>18} {'typ_sl_dist_price':>18} {'typ_sl_pips':>11} {'OK?':>6}")
print("-" * 110)

for ins in cfg["instruments"]:
    broker = cx.resolve_symbol(ins, cfg.get("symbol_map"))
    if broker is None:
        print(f"  {ins}: not on broker"); continue
    info = mt5.symbol_info(broker)
    if info is None: continue

    stops = info.trade_stops_level
    freeze = info.trade_freeze_level
    point = info.point
    pip = point * (10 if info.digits in (3, 5) else 1)
    min_sl_price = stops * point  # min distance in price terms

    # Compute a representative SL distance from the strategy's ATR-based stop
    ltf = cx.fetch_bars(broker, mt5.TIMEFRAME_H1, 800)
    htf = cx.fetch_bars(broker, mt5.TIMEFRAME_H4, 300)
    if len(ltf) < 250 or len(htf) < 60:
        print(f"  {ins}: insufficient bars"); continue
    sig = generate_signals(ltf, htf, DEFAULT_PARAMS)
    sig_with_side = sig[sig["side"].notna()]
    if sig_with_side.empty:
        # use 1.5 * recent ATR as proxy
        from strategy import atr
        a = atr(ltf, 14).iloc[-1]
        typ_sl_price = 1.5 * a
    else:
        # Median |entry - sl| across recent signals
        recent = sig_with_side.tail(40)
        typ_sl_price = (recent["entry"] - recent["sl"]).abs().median()

    typ_sl_pips = typ_sl_price / pip
    ok = "YES" if typ_sl_price > min_sl_price * 2 else ("MARGIN" if typ_sl_price > min_sl_price else "TOO TIGHT")

    print(f"{ins:>10} {broker:>10} {stops:>6} {freeze:>6} {point:>10.5f} "
          f"{pip:>10.5f} {min_sl_price:>18.5f} {typ_sl_price:>18.5f} {typ_sl_pips:>11.1f} {ok:>6}")

cx.shutdown()
