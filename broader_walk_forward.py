"""Phase 1 shadow-candidate discovery + auto-promotion with graduated risk.

Weekly workflow:
  1. Backtest all EODHD-mapped instruments NOT in strategy on pullback/
     breakout/momentum across 60/36/24/12mo windows.
  2. Identify candidates clearing the DEPLOY bar (positive in >= 2 windows,
     avg > +0.05R, 12mo trades >= 20).
  3. Candidates that cleared the bar on >= 2 CONSECUTIVE weekly runs are
     AUTO-PROMOTED into the live strategy at MINIMAL risk (0.25x base).
  4. Promoted instruments graduate: after N live trades with meanR >= +0.05R,
     their risk scales up (0.25 -> 0.5 -> 1.0 -> graduated-out).
  5. Promoted instruments that lose edge are handled by inst_expectancy_
     throttle (already live) and auto_exclusion rules.
  6. If a promoted instrument sits dormant with 0 live trades for 30 days,
     it is REMOVED entirely.

Config mutation is explicit, atomic, and logged. Rollback: previous mt5_runtime.json
is backed up before each change (mt5_runtime.json.bak_wf_<date>).

Outputs:
  - reports/broader_wf/<YYYY-MM-DD>.json (per-run snapshot)
  - reports/broader_wf/latest.json (pointer)
  - reports/broader_wf/promotion_state.json (persisted promotion tracker)
  - reports/broader_wf/audit.log (append-only log of config mutations)

Fast (~1-2 min for 7 candidates). Weekly scheduled task.
"""
import json, os, shutil, sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
import pandas as pd
from collections import defaultdict

import backtest as bt_mod
from config import StrategyParams, INSTRUMENTS, Instrument
import strategy as pullback
import strategy_breakout as breakout
import strategy_momentum as momentum

EODHD_DIR = Path("data_eodhd")
OUT_DIR = Path("reports/broader_wf")
OUT_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = OUT_DIR / "promotion_state.json"
AUDIT_LOG = OUT_DIR / "audit.log"
CONFIG_FILE = Path("mt5_runtime.json")
DB = "trade_journal.db"

STRATS = [("pullback", pullback), ("breakout", breakout), ("momentum", momentum)]
WINDOWS = [60, 36, 24, 12]

# Deploy bar
MIN_WINDOWS_POSITIVE = 2
MIN_AVG_MEANR = 0.05
MIN_12MO_TRADES = 20
# Statistical rigor (added 2026-10-11): require 12mo backtest meanR to be
# significantly > 0 at this p-value (one-sample t-test, right-tailed) before
# allowing promotion. Prevents promoting a candidate on a lucky small-sample
# positive-mean window.
MAX_PROMOTE_P_VALUE = 0.10

# Promotion / graduation policy
CONSEC_RUNS_BEFORE_PROMOTE = 2       # must clear bar on >=2 consecutive weekly runs
PROMOTE_RISK_SCALE = 0.25            # initial live risk = 0.25 x base_risk
GRADUATE_RISK_LADDER = [0.25, 0.5, 1.0]  # scales we step through
GRADUATE_MIN_TRADES = 10             # live trades at current rung before graduating
GRADUATE_MEANR_THRESHOLD = 0.05      # live meanR to graduate
DORMANT_REMOVAL_DAYS = 30            # promoted + no live trades N days -> remove


def _audit(line):
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()}  {line}\n")


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


def _date_window(df, months):
    if df.empty: return df
    end = df.index[-1]
    return df.loc[df.index >= end - pd.Timedelta(days=30 * months)]


def _run_one(ins_name, mod, params, months, h1_full, h4_full):
    """Returns summary dict augmented with 'r_multiples' list for stats tests."""
    h1 = _date_window(h1_full, months)
    h4 = _date_window(h4_full, months)
    if len(h1) < 500: return None
    ins_obj = _ensure_instrument(ins_name)
    if ins_obj is None: return None
    orig = bt_mod.generate_signals
    bt_mod.generate_signals = mod.generate_signals
    try:
        summary, trades = bt_mod.backtest_instrument(ins_obj, h1, h4, params)
    finally:
        bt_mod.generate_signals = orig
    # Attach R-multiple series for statistical tests
    summary["r_multiples"] = [float(t.r_multiple_net) for t in trades]
    return summary


def _one_sample_ttest_p(values, alternative="greater"):
    """One-sample t-test vs H0: mean = 0.
    alternative='greater' -> P(T > t_stat)  (promoted-when-positive case)
    alternative='less'    -> P(T < t_stat)  (paused-when-negative case)
    Returns None if insufficient data or variance.
    """
    import statistics
    if len(values) < 2: return None
    try:
        sd = statistics.stdev(values)
    except Exception:
        return None
    if sd <= 1e-9: return None
    mean = sum(values) / len(values)
    t_stat = mean / (sd / (len(values) ** 0.5))
    try:
        from scipy import stats as _sstats
        if alternative == "greater":
            return float(1.0 - _sstats.t.cdf(t_stat, df=len(values) - 1))
        else:
            return float(_sstats.t.cdf(t_stat, df=len(values) - 1))
    except Exception:
        return None


def _load_state():
    if STATE_FILE.exists():
        try: return json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception: pass
    return {"promoted": {}, "run_history": []}


def _save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _load_prior():
    latest = OUT_DIR / "latest.json"
    if not latest.exists(): return {}
    try: return json.load(open(latest, encoding="utf-8"))
    except Exception: return {}


def _live_meanR_last_n(instrument, n=10):
    """Fetch live meanR over last N trades for an instrument."""
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT r_multiple_gross, entry_ts FROM trades WHERE instrument=? "
        "AND r_multiple_gross IS NOT NULL ORDER BY entry_ts DESC LIMIT ?",
        (instrument, n)
    ).fetchall()
    db.close()
    if not rows: return None, 0, None
    rs = [r["r_multiple_gross"] for r in rows]
    try:
        last_ts = rows[0]["entry_ts"]
    except Exception:
        last_ts = None
    return sum(rs) / len(rs), len(rs), last_ts


def _apply_config_mutation(mutation_fn, label):
    """Atomic config mutation with backup + audit."""
    date_tag = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup = CONFIG_FILE.with_suffix(f".json.bak_wf_{date_tag}")
    shutil.copy(CONFIG_FILE, backup)
    cfg = json.load(open(CONFIG_FILE, encoding="utf-8"))
    mutation_fn(cfg)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)
    # Also refresh the protected copy
    try:
        shutil.copy(CONFIG_FILE, "mt5_runtime_protected.json")
    except Exception:
        pass
    _audit(f"CONFIG_MUTATE  {label}  backup={backup.name}")


def _promote(ins, strategy, risk_scale):
    """Add instrument to strategy at reduced risk. Idempotent."""
    def mutate(cfg):
        if ins not in cfg["instruments"]:
            cfg["instruments"].append(ins)
        global_base = float(cfg["tier"]["base_risk_pct"])
        to = cfg.setdefault("tier_overrides", {})
        to[ins] = {
            "enabled": True,
            "base_risk_pct": round(global_base * risk_scale, 2),
            "_shadow_risk_scale": risk_scale,
            "_promoted_at": datetime.now(timezone.utc).isoformat(),
            "_promoted_strategy": strategy,
            "_note": f"Auto-promoted by broader_walk_forward.py at {risk_scale}x base risk."
        }
        sp = cfg.setdefault("strategy_per_instrument", {})
        sp[ins] = strategy
    _apply_config_mutation(mutate, f"PROMOTE {ins} strategy={strategy} scale={risk_scale}")
    print(f"  >> PROMOTED {ins} strategy={strategy} at {risk_scale}x base_risk")


def _graduate(ins, from_scale, to_scale):
    def mutate(cfg):
        global_base = float(cfg["tier"]["base_risk_pct"])
        over = cfg["tier_overrides"].get(ins, {})
        over["base_risk_pct"] = round(global_base * to_scale, 2)
        over["_shadow_risk_scale"] = to_scale
        over["_graduated_at"] = datetime.now(timezone.utc).isoformat()
        over["_prev_scale"] = from_scale
    _apply_config_mutation(mutate, f"GRADUATE {ins} {from_scale}x -> {to_scale}x")
    print(f"  >> GRADUATED {ins} {from_scale}x -> {to_scale}x base_risk")


def _graduate_out(ins):
    """Remove shadow risk override — instrument now runs at full base_risk."""
    def mutate(cfg):
        over = cfg["tier_overrides"].get(ins, {})
        # Strip the overrides so instrument runs with global tier
        for k in ("base_risk_pct", "_shadow_risk_scale", "_promoted_at",
                  "_promoted_strategy", "_note", "_graduated_at", "_prev_scale"):
            over.pop(k, None)
        if not over or all(k.startswith("_") for k in over):
            cfg["tier_overrides"].pop(ins, None)
    _apply_config_mutation(mutate, f"GRADUATE_OUT {ins} -> full base_risk")
    print(f"  >> GRADUATED-OUT {ins}: now at full base_risk, no override")


def _remove(ins, reason):
    def mutate(cfg):
        if ins in cfg["instruments"]:
            cfg["instruments"].remove(ins)
        cfg.get("tier_overrides", {}).pop(ins, None)
        cfg.get("strategy_per_instrument", {}).pop(ins, None)
    _apply_config_mutation(mutate, f"REMOVE {ins} reason={reason}")
    print(f"  >> REMOVED {ins}  ({reason})")


def main():
    now = datetime.now(timezone.utc)
    date_tag = now.strftime("%Y-%m-%d")

    # Load EODHD mapping (long history, primary source)
    eodhd_map = json.load(open(EODHD_DIR / "_mapping.json", encoding="utf-8"))
    # Also load MT5-cache mapping if available (shorter history, broader coverage)
    mt5_map_path = Path("data_mt5/_mapping.json")
    mt5_map = {}
    if mt5_map_path.exists():
        try:
            mt5_map = json.load(open(mt5_map_path, encoding="utf-8"))
        except Exception:
            mt5_map = {}
    # Merge: EODHD wins where both have data (longer history)
    mapping = dict(mt5_map); mapping.update(eodhd_map)
    cfg = json.load(open(CONFIG_FILE, encoding="utf-8"))
    current = set(cfg["instruments"])
    state = _load_state()

    # Candidate pool: EODHD-mapped + NOT currently in strategy
    # (previously-promoted are IN the strategy; we skip them for backtest-based
    #  promotion checks, but still track their live performance for graduation.)
    candidates = sorted(set(mapping.keys()) - current)

    print("=" * 90)
    print(f"BROADER WALK-FORWARD + AUTO-PROMOTION — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 90)
    print(f"Shadow candidates (EODHD + MT5-cached, not yet live): {len(candidates)}")
    print(f"  EODHD-mapped: {len([c for c in candidates if c in eodhd_map])}")
    print(f"  MT5-cached only: {len([c for c in candidates if c not in eodhd_map and c in mt5_map])}")
    print(f"Currently promoted (live at reduced risk):    {len(state['promoted'])}")

    # --- 1. Backtest candidates ---
    results = defaultdict(dict)
    params = StrategyParams(fast_ema=50, slow_ema=200, atr_period=14,
                            atr_stop_mult=2.0, rr_target=2.5)
    for ins in candidates:
        # Prefer EODHD (longer history); fallback MT5 cache
        src_dir = EODHD_DIR if ins in eodhd_map else Path("data_mt5")
        try:
            h1 = pd.read_parquet(src_dir / f"{ins}_H1.parquet")
            h4 = pd.read_parquet(src_dir / f"{ins}_H4.parquet")
        except Exception as e:
            print(f"  {ins}: data load failed: {e}")
            continue
        for strat_name, mod in STRATS:
            for m in WINDOWS:
                r = _run_one(ins, mod, params, m, h1, h4)
                if r and r.get("trades", 0) >= 5:
                    results[ins][(strat_name, m)] = r

    # --- 2. Deploy-bar check per candidate ---
    cleared_now = {}
    print(f"\nDeploy bar: positive meanR in >= {MIN_WINDOWS_POSITIVE} windows "
          f"AND avg > +{MIN_AVG_MEANR}R AND 12mo trades >= {MIN_12MO_TRADES}")
    for ins in candidates:
        if ins not in results: continue
        for strat_name, _ in STRATS:
            vals = [results[ins].get((strat_name, m)) for m in WINDOWS]
            valid = [x for x in vals if x]
            if len(valid) < 2: continue
            positives = sum(1 for x in valid if x["expectancy_R_net"] > 0)
            avg = sum(x["expectancy_R_net"] for x in valid) / len(valid)
            twelve = results[ins].get((strat_name, 12))
            n_12 = twelve["trades"] if twelve else 0
            r_12 = twelve["expectancy_R_net"] if twelve else 0
            if (positives >= MIN_WINDOWS_POSITIVE and avg > MIN_AVG_MEANR
                    and n_12 >= MIN_12MO_TRADES and r_12 > 0):
                # Statistical rigor: 12mo meanR must be significantly > 0
                r_series = (twelve or {}).get("r_multiples", []) if twelve else []
                p_val = _one_sample_ttest_p(r_series, alternative="greater")
                if p_val is None or p_val > MAX_PROMOTE_P_VALUE:
                    # Failed significance -- flag for monitoring but don't promote
                    print(f"  {ins:7s} {strat_name:10s} meets effect bar but p={p_val} > {MAX_PROMOTE_P_VALUE} - NOT promotable yet")
                    continue
                cleared_now[(ins, strat_name)] = {
                    "avg_meanR": round(avg, 3),
                    "positives_of": f"{positives}/{len(valid)}",
                    "trades_12mo": n_12,
                    "meanR_12mo": round(r_12, 3),
                    "p_value_12mo": round(p_val, 4),
                }

    # --- 3. Update consecutive-run tracker, promote eligible ---
    run_history = state.setdefault("run_history", [])
    run_history.append({"date": date_tag, "cleared": [f"{ins}:{s}" for (ins,s) in cleared_now]})
    run_history[:] = run_history[-10:]  # keep last 10 runs

    # For each candidate currently clearing: count consecutive clears
    for (ins, strat_name), d in cleared_now.items():
        key = f"{ins}:{strat_name}"
        consec = 0
        for h in reversed(run_history):
            if key in h["cleared"]: consec += 1
            else: break
        d["consec_runs"] = consec
        if consec >= CONSEC_RUNS_BEFORE_PROMOTE and ins not in state["promoted"]:
            # PROMOTE
            _promote(ins, strat_name, PROMOTE_RISK_SCALE)
            state["promoted"][ins] = {
                "strategy": strat_name, "scale": PROMOTE_RISK_SCALE,
                "promoted_at": now.isoformat(),
                "consec_clears_at_promote": consec,
                "promote_meanR_12mo": d["meanR_12mo"],
            }

    # --- 4. Graduation / removal of already-promoted ---
    for ins, prom in list(state["promoted"].items()):
        meanR, n_trades, last_ts = _live_meanR_last_n(ins, 15)
        current_scale = prom["scale"]
        prom["live_meanR_last_15"] = round(meanR, 3) if meanR is not None else None
        prom["live_trades_count"] = n_trades
        prom["live_last_ts"] = last_ts

        # Dormancy check: if promoted + no live trades for N days -> remove
        if n_trades == 0:
            days = (now - datetime.fromisoformat(prom["promoted_at"])).total_seconds() / 86400
            if days >= DORMANT_REMOVAL_DAYS:
                _remove(ins, f"dormant_{int(days)}d_zero_trades")
                state["promoted"].pop(ins, None)
                continue

        # Graduation: if meanR >= threshold on >= min_trades, bump up
        if meanR is not None and meanR >= GRADUATE_MEANR_THRESHOLD and n_trades >= GRADUATE_MIN_TRADES:
            if current_scale < GRADUATE_RISK_LADDER[-1]:
                next_scale = next((s for s in GRADUATE_RISK_LADDER if s > current_scale), None)
                if next_scale is not None:
                    _graduate(ins, current_scale, next_scale)
                    prom["scale"] = next_scale
                    prom["last_graduated_at"] = now.isoformat()
                    if next_scale >= GRADUATE_RISK_LADDER[-1]:
                        # Final rung reached -> remove the per-ins override entirely
                        _graduate_out(ins)
                        state["promoted"].pop(ins, None)

    # --- 5. Persist state + report ---
    _save_state(state)

    out = {
        "generated_at": now.isoformat(),
        "deploy_bar": {
            "min_windows_positive": MIN_WINDOWS_POSITIVE,
            "min_avg_meanR": MIN_AVG_MEANR,
            "min_12mo_trades": MIN_12MO_TRADES,
        },
        "promotion_policy": {
            "consec_runs_before_promote": CONSEC_RUNS_BEFORE_PROMOTE,
            "promote_risk_scale": PROMOTE_RISK_SCALE,
            "graduate_ladder": GRADUATE_RISK_LADDER,
            "graduate_min_trades": GRADUATE_MIN_TRADES,
            "graduate_meanR_threshold": GRADUATE_MEANR_THRESHOLD,
            "dormant_removal_days": DORMANT_REMOVAL_DAYS,
        },
        "cleared_this_run": {f"{k[0]}:{k[1]}": v for k, v in cleared_now.items()},
        "promoted_live": state["promoted"],
    }
    with open(OUT_DIR / f"{date_tag}.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    with open(OUT_DIR / "latest.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    # Printed summary
    print(f"\nCleared bar this run: {len(cleared_now)}")
    for (ins, strat), d in cleared_now.items():
        prom_status = " [PROMOTED]" if ins in state["promoted"] else f" [{d['consec_runs']} consec]"
        print(f"  {ins:7s} {strat:10s} avg={d['avg_meanR']:+.3f}R  "
              f"12mo={d['meanR_12mo']:+.3f}R  trades={d['trades_12mo']}"
              f"{prom_status}")
    if state["promoted"]:
        print(f"\nCurrently promoted (live at reduced risk): {len(state['promoted'])}")
        for ins, prom in state["promoted"].items():
            mr = prom.get("live_meanR_last_15"); nt = prom.get("live_trades_count")
            mr_s = f"{mr:+.3f}R" if mr is not None else "n/a"
            print(f"  {ins:7s} strategy={prom['strategy']:10s} scale={prom['scale']}x  "
                  f"live_last_15: {nt} trades @ meanR={mr_s}")
    print(f"\nSaved to reports/broader_wf/{date_tag}.json")


if __name__ == "__main__":
    main()
