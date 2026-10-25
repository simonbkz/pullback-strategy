"""Statistical audit of current strategy_per_instrument assignments.

For each explicitly-assigned instrument (not the default 'pullback' fallback),
run a one-sample t-test against H0: meanR = 0 on 12mo EODHD backtest data
under the ASSIGNED strategy. Report:
  - Instrument / strategy / 12mo meanR / stdev / p-value
  - Verdict against pause-level rigor (p <= 0.10 for 'significantly positive')

For 'ensemble' instruments, the ensemble generator is used (any of 3 fires).

No config mutation — pure analysis. Flags any assignment that doesn't clear
today's statistical bar for user decision.
"""
import json
import sys
from pathlib import Path
import pandas as pd
import statistics
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import backtest as bt_mod
from config import StrategyParams, INSTRUMENTS, Instrument
import strategy as pullback
import strategy_breakout as breakout
import strategy_momentum as momentum
import live_trader as lt

EODHD_DIR = Path("data_eodhd")
WINDOW_MONTHS = 12
P_THRESHOLD = 0.10


def _ensure_instrument(name):
    existing = next((i for i in INSTRUMENTS if i.name == name), None)
    if existing: return existing
    try:
        return Instrument(
            name=name, broker_symbol=name, kind="forex",
            pip_size=0.01 if name.endswith("JPY") else 0.0001,
            contract_size=100000.0, session=None,
        )
    except Exception:
        return None


def _get_strategy_fn(strat_name):
    if strat_name == "pullback": return pullback.generate_signals
    if strat_name == "breakout": return breakout.generate_signals
    if strat_name == "momentum": return momentum.generate_signals
    if strat_name == "ensemble": return lt._generate_ensemble_signal
    return None


def _date_window(df, months):
    end = df.index[-1]
    return df.loc[df.index >= end - pd.Timedelta(days=30 * months)]


def run_test(ins_name, strat_name, params, months, h1_full, h4_full):
    h1 = _date_window(h1_full, months); h4 = _date_window(h4_full, months)
    if len(h1) < 500: return None
    ins_obj = _ensure_instrument(ins_name)
    if ins_obj is None: return None
    fn = _get_strategy_fn(strat_name)
    if fn is None: return None
    orig = bt_mod.generate_signals
    bt_mod.generate_signals = fn
    try:
        summary, trades = bt_mod.backtest_instrument(ins_obj, h1, h4, params)
    finally:
        bt_mod.generate_signals = orig
    rs = [float(t.r_multiple_net) for t in trades]
    return summary, rs


def main():
    cfg = json.load(open("mt5_runtime.json", encoding="utf-8"))
    mapping = json.load(open(EODHD_DIR / "_mapping.json", encoding="utf-8"))
    sp = cfg.get("strategy_per_instrument", {}) or {}
    disabled = set()
    for ins, v in cfg.get("tier_overrides", {}).items():
        if isinstance(v, dict) and v.get("enabled") is False:
            disabled.add(ins)

    params = StrategyParams(fast_ema=50, slow_ema=200, atr_period=14,
                            atr_stop_mult=2.0, rr_target=2.5)

    print("=" * 110)
    print(f"STATISTICAL AUDIT — assigned strategies vs {WINDOW_MONTHS}mo backtest, "
          f"H0: meanR = 0  |  bar: p <= {P_THRESHOLD}")
    print("=" * 110)
    print(f"\n{'Instrument':10s} {'Strategy':10s} {'n':>5s} {'meanR':>8s} {'stdev':>7s} "
          f"{'t-stat':>7s} {'p-val':>8s}  Verdict")

    findings = []
    # Iterate the EXPLICITLY-assigned entries only (default pullback is tested separately)
    for ins, strat in sp.items():
        if ins.startswith("_"): continue
        if not isinstance(strat, str) or strat.startswith("_"): continue
        if ins in disabled: continue
        if ins not in mapping:
            print(f"  {ins:10s} {strat:10s} (no EODHD data — skipped)")
            continue
        try:
            h1 = pd.read_parquet(EODHD_DIR / f"{ins}_H1.parquet")
            h4 = pd.read_parquet(EODHD_DIR / f"{ins}_H4.parquet")
        except Exception as e:
            print(f"  {ins:10s} load fail: {e}")
            continue
        res = run_test(ins, strat, params, WINDOW_MONTHS, h1, h4)
        if res is None:
            print(f"  {ins:10s} {strat:10s} (thin backtest)")
            continue
        summary, rs = res
        if len(rs) < 10:
            print(f"  {ins:10s} {strat:10s} n={len(rs)} (too thin)")
            continue
        m = statistics.mean(rs)
        sd = statistics.stdev(rs) if len(rs) > 1 else 0.0
        if sd < 1e-9:
            t_stat = 0.0; p = 1.0 if m <= 0 else 0.0
        else:
            t_stat = m / (sd / len(rs) ** 0.5)
            # right-tailed: we want evidence meanR > 0
            p = 1.0 - float(stats.t.cdf(t_stat, df=len(rs) - 1))
        verdict = ""
        if m <= 0:
            verdict = "FAIL: meanR <= 0"
            findings.append((ins, strat, m, p, verdict))
        elif p <= P_THRESHOLD:
            verdict = f"PASS (p={p:.3f})"
        else:
            verdict = f"NOT SIGNIFICANT (p={p:.3f}, meanR {m:+.3f}R)"
            findings.append((ins, strat, m, p, verdict))
        print(f"  {ins:10s} {strat:10s} {len(rs):>5d} {m:>+7.3f}R {sd:>6.2f}R "
              f"{t_stat:>+6.2f} {p:>8.4f}  {verdict}")

    print(f"\n{'=' * 110}")
    print("SUMMARY — assignments that would NOT clear today's statistical bar:")
    print(f"{'=' * 110}")
    if findings:
        for ins, strat, m, p, v in findings:
            print(f"  {ins:10s} strategy={strat:10s} meanR={m:+.3f}R  p={p:.4f}  --> {v}")
        print(f"\n{len(findings)} of the explicitly-assigned instruments fail the bar.")
        print("Decision for user: these may be worth revisiting (either switch strategy or disable).")
    else:
        print("  All explicit assignments clear the bar.")


if __name__ == "__main__":
    main()
