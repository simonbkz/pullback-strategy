"""For every enabled instrument with zero live signals, find which strategy (if any)
has statistically-gated edge, and whether the live silence is explained by low
signal frequency or by something blocking.

Per instrument x strategy:
  - 60/36/24/12mo meanR (EODHD -> data_mt5 -> live MT5 fetch fallback)
  - 12mo one-sample t-test p (right-tailed)
  - stat gate: p<=0.10 AND positive in >=2 windows AND avg>+0.05R AND 12mo n>=20
  - expected signals in the LAST 90 DAYS on the CURRENT assigned strategy
    (if backtest shows ~0 in 90d, live 0 is consistent; if it shows 10+, something blocks)
"""
import sys, json, statistics
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd
from datetime import datetime, timezone, timedelta

import broader_walk_forward as bwf           # _ensure_instrument, _run_one, _one_sample_ttest_p, _date_window
from config import StrategyParams
import strategy as pullback, strategy_breakout as breakout, strategy_momentum as momentum
import live_trader as lt
import backtest as bt_mod
import MetaTrader5 as mt5

class _Ens:
    @staticmethod
    def generate_signals(l, h, p): return lt._generate_ensemble_signal(l, h, p)
STRATS = [("pullback", pullback), ("breakout", breakout), ("momentum", momentum), ("ensemble", _Ens)]
WINDOWS = [60, 36, 24, 12]
PARAMS = StrategyParams(fast_ema=50, slow_ema=200, atr_period=14, atr_stop_mult=2.0, rr_target=2.5)
P_MAX, MIN_WIN_POS, MIN_AVG, MIN_N12 = 0.10, 2, 0.05, 20

cfg = json.load(open("mt5_runtime.json", encoding="utf-8"))
sp = cfg.get("strategy_per_instrument", {})
eod = json.load(open("data_eodhd/_mapping.json", encoding="utf-8"))
m5p = Path("data_mt5/_mapping.json"); m5 = json.load(open(m5p, encoding="utf-8")) if m5p.exists() else {}

DORMANT = ["CHFJPY","GBPUSD","EURUSD","US30","EURJPY","USDCAD","USDJPY","AUDUSD",
           "CADJPY","NZDJPY","USDCHF","NZDUSD","GBPJPY","AUDJPY","FRA40"]

def load(ins):
    for d, src in [(Path("data_eodhd"), "EODHD"), (Path("data_mt5"), "MT5cache")]:
        if (d / f"{ins}_H1.parquet").exists():
            return pd.read_parquet(d / f"{ins}_H1.parquet"), pd.read_parquet(d / f"{ins}_H4.parquet"), src
    # live fetch fallback
    if not mt5.initialize(): return None, None, None
    sym = next((c for c in cfg["symbol_map"].get(ins, [ins, ins+"r"]) if mt5.symbol_info(c)), None)
    if not sym: return None, None, None
    r = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_H1, 0, 5)
    now = int(datetime.now(timezone.utc).timestamp()); off = round((int(r[-1]['time']) - (now-now%3600))/3600) if r is not None and len(r) else 0
    rates = mt5.copy_rates_range(sym, mt5.TIMEFRAME_H1, datetime.now(timezone.utc)-timedelta(days=365*5+off/24), datetime.now(timezone.utc))
    if rates is None or len(rates) < 2000: return None, None, None
    df = pd.DataFrame(rates); df["time"] = pd.to_datetime(df["time"], unit="s", utc=True) - pd.Timedelta(hours=off)
    df = df.set_index("time").sort_index().rename(columns={"tick_volume":"volume"})[["open","high","low","close","volume"]]
    h4 = df.resample("4h", origin="epoch", label="left", closed="left").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()
    return df, h4, f"MT5live({sym})"

def count_signals_90d(ins, mod, h1, h4):
    """How many entry signals would the strategy have produced in the last 90d of data?"""
    end = h1.index[-1]; h1w = h1.loc[h1.index >= end - pd.Timedelta(days=90+60)]; h4w = h4.loc[h4.index >= end - pd.Timedelta(days=90+60)]
    try:
        sig = mod.generate_signals(h1w, h4w, PARAMS)
        sig = sig.loc[sig.index >= end - pd.Timedelta(days=90)]
        return int(sig["side"].isin(["long","short"]).sum()), str(end)[:10]
    except Exception as e:
        return -1, str(e)[:30]

print("="*110)
print("DORMANT-INSTRUMENT STRATEGY SEARCH  (gate: p<=0.10, >=2/4 windows +, avg>+0.05R, 12mo n>=20)")
print("="*110)
results = {}
for ins in DORMANT:
    h1, h4, src = load(ins)
    cur = sp.get(ins, "pullback"); cur = "pullback" if cur.startswith("_") else cur
    if h1 is None:
        print(f"\n{ins}  current={cur}  NO DATA — skipped"); continue
    print(f"\n{ins}  current={cur}  data={src} ({h1.index[0]:%Y-%m-%d}→{h1.index[-1]:%Y-%m-%d})")
    print(f"  {'strategy':10s} {'60mo':>13s} {'36mo':>13s} {'24mo':>13s} {'12mo':>13s} {'p12':>6s} {'avg':>7s}  gate")
    best = None
    for name, mod in STRATS:
        cells = []; vals = []
        for m in WINDOWS:
            r = bwf._run_one(ins, mod, PARAMS, m, h1, h4)
            if r and r.get("trades",0) >= 5:
                cells.append(f"{r['expectancy_R_net']:+.3f}({r['trades']:>3d})"); vals.append(r)
            else: cells.append("thin")
        if len(vals) < 2:
            print(f"  {name:10s} " + " ".join(f"{c:>13s}" for c in cells) + "   (thin)"); continue
        pos = sum(1 for v in vals if v["expectancy_R_net"] > 0); avg = sum(v["expectancy_R_net"] for v in vals)/len(vals)
        r12 = next((v for v,m in zip(vals,[w for w,c in zip(WINDOWS,cells) if c!="thin"]) if m==12), None)
        p12 = bwf._one_sample_ttest_p(r12["r_multiples"], "greater") if r12 else None
        n12 = r12["trades"] if r12 else 0; e12 = r12["expectancy_R_net"] if r12 else 0
        gate = (p12 is not None and p12 <= P_MAX and pos >= MIN_WIN_POS and avg > MIN_AVG and n12 >= MIN_N12 and e12 > 0)
        ps = f"{p12:.3f}" if p12 is not None else "  n/a"
        print(f"  {name:10s} " + " ".join(f"{c:>13s}" for c in cells) + f" {ps:>6s} {avg:>+7.3f}  {'PASS' if gate else ('near' if (pos>=2 and avg>0) else '-')}")
        if gate and (best is None or avg > best[1]): best = (name, avg, p12, n12, e12)
    # signal frequency on CURRENT strategy
    cur_mod = dict(STRATS)[cur] if cur in dict(STRATS) else pullback
    n90, asof = count_signals_90d(ins, cur_mod, h1, h4)
    freq_note = ("CONSISTENT with live silence" if 0 <= n90 <= 2 else ("SUSPICIOUS — backtest fires but live doesn't" if n90 >= 6 else "low"))
    print(f"  signals last 90d on '{cur}' (data to {asof}): {n90}  → {freq_note}")
    results[ins] = {"current": cur, "best_gated": best, "sig90_current": n90, "src": src}

print("\n"+"="*110); print("SUMMARY — proposed changes"); print("="*110)
for ins, r in results.items():
    b = r["best_gated"]
    if b and b[0] != r["current"]:
        print(f"  {ins:8s} {r['current']:9s} -> {b[0]:9s}  avg={b[1]:+.3f}R  p12={b[2]:.3f}  n12={b[3]}  12mo={b[4]:+.3f}R")
    elif b:
        print(f"  {ins:8s} keep {r['current']:9s} (already best gated; silence = {'low freq' if r['sig90_current']<=2 else 'INVESTIGATE'})")
    else:
        print(f"  {ins:8s} NO strategy clears gate  (current {r['current']}, sig90={r['sig90_current']})")
try: mt5.shutdown()
except Exception: pass
