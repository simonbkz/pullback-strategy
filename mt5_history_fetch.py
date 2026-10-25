"""Pull H1 + H4 historical data for broker FX symbols NOT already covered by EODHD.

Expands the testable universe for broader_walk_forward.py so the stricter
statistical gate has more candidates to evaluate.

Writes:
  data_mt5/<INS>_H1.parquet
  data_mt5/<INS>_H4.parquet (resampled from H1)
  data_mt5/_mapping.json  (list of symbols covered)

Idempotent: skips instruments already in EODHD mapping or already cached today.
Pulls up to 5 years of H1 bars where available.

Designed as a weekly or monthly refresh. Can run manually: python mt5_history_fetch.py
"""
import json, time, os
from pathlib import Path
from datetime import datetime, timezone, timedelta
import pandas as pd
import MetaTrader5 as mt5

if not mt5.initialize():
    print("mt5 init fail", mt5.last_error()); raise SystemExit

OUT_DIR = Path("data_mt5")
OUT_DIR.mkdir(parents=True, exist_ok=True)
EODHD_DIR = Path("data_eodhd")

LOOKBACK_YEARS = 5
MIN_BARS_H1 = 2000  # need at least ~2000 H1 bars to be useful for backtesting

eodhd_map = {}
try:
    eodhd_map = json.load(open(EODHD_DIR / "_mapping.json", encoding="utf-8"))
except Exception:
    pass

cfg = json.load(open("mt5_runtime.json", encoding="utf-8"))
current_universe = set(cfg["instruments"])

# Load broker FX symbols
all_syms = mt5.symbols_get() or []
# Normalize: HFM uses lowercase "r" suffix (e.g., EURUSDr). We want the base name.
fx_bases = set()
for s in all_syms:
    if not s.path: continue
    if not s.path.startswith(("Forex", "Forex CFD Pro")): continue
    name = s.name
    # Strip "r" suffix if present (HFM convention)
    base = name[:-1] if name.endswith("r") and len(name) == 7 else name
    # Only standard 6-char pairs (EURUSD, GBPJPY, etc.)
    if len(base) == 6 and base.isalpha():
        fx_bases.add(base.upper())

print(f"Broker exposes {len(fx_bases)} FX pair bases")
# Exclude: already in strategy, already EODHD-mapped
need = [b for b in sorted(fx_bases)
        if b not in current_universe and b not in eodhd_map]
print(f"EODHD-mapped: {len(eodhd_map)}  current universe: {len(current_universe)}")
print(f"To fetch MT5 history for: {len(need)}")


def resolve_symbol(base):
    """Return broker-side symbol name for a base FX pair."""
    for cand in [base, base + "r", "#" + base, "#" + base + "r"]:
        info = mt5.symbol_info(cand)
        if info is not None:
            return cand
    return None


def broker_offset(sym):
    info = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_H1, 0, 5)
    if info is None or len(info) == 0: return 0
    last = int(info[-1]['time']); now = int(datetime.now(timezone.utc).timestamp())
    return round((last - (now - (now % 3600))) / 3600)


existing_map = {}
if (OUT_DIR / "_mapping.json").exists():
    try:
        existing_map = json.load(open(OUT_DIR / "_mapping.json", encoding="utf-8"))
    except Exception:
        pass

new_map = dict(existing_map)
n_ok = 0; n_skip = 0; n_fail = 0

now = datetime.now(timezone.utc)
start = now - timedelta(days=365 * LOOKBACK_YEARS)

for base in need:
    out_h1 = OUT_DIR / f"{base}_H1.parquet"
    out_h4 = OUT_DIR / f"{base}_H4.parquet"
    # Skip if cached today
    if out_h1.exists() and out_h4.exists():
        age = (datetime.now() - datetime.fromtimestamp(out_h1.stat().st_mtime)).total_seconds()
        if age < 86400 * 3:  # 3 days
            n_skip += 1
            new_map[base] = existing_map.get(base, {"broker_symbol": resolve_symbol(base)})
            continue
    sym = resolve_symbol(base)
    if sym is None:
        n_fail += 1; continue
    off = broker_offset(sym)
    rates = mt5.copy_rates_range(sym, mt5.TIMEFRAME_H1,
                                   start - timedelta(hours=off),
                                   now - timedelta(hours=off))
    if rates is None or len(rates) < MIN_BARS_H1:
        print(f"  {base:7s} ({sym}): only {0 if rates is None else len(rates)} bars — skip")
        n_fail += 1; continue
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True) - pd.Timedelta(hours=off)
    df = df.set_index("time").sort_index()
    # Rename columns to match EODHD schema
    df = df.rename(columns={"tick_volume": "volume"})
    df_h1 = df[["open", "high", "low", "close", "volume"]].copy()
    # H4 = resample H1 using institutional-aligned labels (00/04/08/12/16/20)
    df_h4 = df_h1.resample("4h", origin="epoch", label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    }).dropna()
    df_h1.to_parquet(out_h1)
    df_h4.to_parquet(out_h4)
    new_map[base] = {"broker_symbol": sym, "source": "mt5", "last_fetched": now.isoformat()}
    print(f"  {base:7s} ({sym}): {len(df_h1)} H1 bars, {len(df_h4)} H4 bars")
    n_ok += 1
    time.sleep(0.1)  # gentle on broker

with open(OUT_DIR / "_mapping.json", "w", encoding="utf-8") as f:
    json.dump(new_map, f, indent=2)

print(f"\nDone: {n_ok} fetched  {n_skip} skipped (cached)  {n_fail} failed")
print(f"MT5-cached universe now: {len(new_map)} symbols")
mt5.shutdown()
