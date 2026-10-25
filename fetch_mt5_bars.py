"""Fetch H1 + H4 historical bars directly from MT5 for instruments not in
yfinance (bonds, additional indices, etc). Saves to data/ as parquet.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone, timedelta
import pandas as pd
import MetaTrader5 as mt5
import json
import live_mt5_connector as cx
from config import DATA_DIR


# New cross-asset diversifiers identified via _discover_hfm_symbols.py
NEW_SYMBOLS = [
    # logical name, broker symbol, asset class, pip_size, typical spread (pips)
    ("US10Y",  "US10YR.F", "bond",      0.01,   2.0),
    ("EUBUND", "EUBUND.F", "bond",      0.01,   2.0),
    ("UKGILT", "UKGILT.F", "bond",      0.01,   2.0),
    ("US30",   "US30.F",   "index",     0.1,    30.0),
    ("UK100",  "UK100",    "index",     0.1,    10.0),
    ("AUS200", "AUS200",   "index",     0.1,    10.0),
    ("FRA40",  "FRA40",    "index",     0.1,    10.0),
    ("EU50",   "EU50.F",   "index",     0.1,    10.0),
    ("COPPER", "Copper",   "commodity", 0.0001, 5.0),
    ("PLATIN", "Platinum", "commodity", 0.10,   5.0),
]
DAYS = 730  # 2 years of history


def fetch_and_save(broker_sym: str, name: str, tf, label: str, days: int = DAYS) -> bool:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    rates = mt5.copy_rates_range(broker_sym, tf, start, end)
    if rates is None or len(rates) == 0:
        print(f"  [{name}] {label}: NO DATA from MT5 ({mt5.last_error()})")
        return False
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("time")[["open", "high", "low", "close"]]
    df.index.name = "ts"
    os.makedirs(DATA_DIR, exist_ok=True)
    out = f"{DATA_DIR}/{name}_{label}.parquet"
    df.to_parquet(out)
    print(f"  [{name}] {label}: {len(df)} bars  [{df.index[0]} -> {df.index[-1]}]  -> {out}")
    return True


def main() -> int:
    cfg = json.load(open("mt5_runtime.json"))
    if not cx.connect(path=cfg["connection"]["path"],
                       expected_server=cfg["connection"]["expected_server"],
                       expected_login=cfg["connection"]["expected_login"]):
        print("connect failed")
        return 1

    success = []
    for name, broker, asset_class, pip_size, spread_pips in NEW_SYMBOLS:
        ok_h1 = fetch_and_save(broker, name, mt5.TIMEFRAME_H1, "1h")
        ok_h4 = fetch_and_save(broker, name, mt5.TIMEFRAME_H4, "4h")
        if ok_h1 and ok_h4:
            success.append((name, broker, asset_class, pip_size, spread_pips))

    print(f"\nSuccessfully fetched {len(success)}/{len(NEW_SYMBOLS)} instruments:")
    for n, b, ac, ps, sp in success:
        print(f"  Instrument(\"{b}\", \"{n}\", \"{ac}\", {ps}, {sp})")
    cx.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
