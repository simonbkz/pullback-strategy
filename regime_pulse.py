"""Regime pulse — daily feedback loop on the strategy's live state.

Runs journal queries (no MT5 needed, no network) and produces:
  1. Per-instrument expectancy over rolling 20-trade windows
  2. Portfolio health: WR, meanR, mean_win, mean_loss drift
  3. Outlier losses needing root-cause review (|R| > 1.5)
  4. Signal frequency delta per strategy type (recent vs baseline)
  5. Correlation coherence: same-day same-direction clusters

Outputs:
  - stdout: human-readable summary
  - regime_pulse.json: machine-readable snapshot (overwrites each run)
  - reports/regime_pulse_<YYYYMMDD>.json: dated archive

Fast (< 5s), reproducible, journal-driven. Designed as a scheduled task.
"""
import sqlite3, json, os
from datetime import datetime, timezone, timedelta
from collections import defaultdict, Counter

DB = "trade_journal.db"
CONFIG = "mt5_runtime.json"
RECENT_WINDOW_TRADES = 20       # per-instrument rolling window
RECENT_WINDOW_DAYS = 14         # portfolio rolling window
BASELINE_WINDOW_DAYS = 60       # for drift comparison
OUTLIER_R = 1.5                 # loss severity threshold


def _summarize(rs):
    if not rs:
        return {"n": 0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    mw = sum(wins) / len(wins) if wins else 0
    ml = sum(losses) / len(losses) if losses else 0
    wr = 100 * len(wins) / len(rs)
    rr = abs(mw / ml) if ml else 0
    be_wr = 100 / (1 + rr) if rr else 0
    return {
        "n": len(rs),
        "wr_pct": round(wr, 1),
        "mean_win_R": round(mw, 3),
        "mean_loss_R": round(ml, 3),
        "rr": round(rr, 2),
        "be_wr_pct": round(be_wr, 1),
        "mean_R": round(sum(rs) / len(rs), 3),
        "edge_vs_be": round(wr - be_wr, 1),  # positive = profitable
    }


def main():
    now = datetime.now(timezone.utc)
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row

    cfg = json.load(open(CONFIG, "r", encoding="utf-8"))
    cfg_instruments = cfg.get("instruments", [])
    cfg_strat = cfg.get("strategy_per_instrument", {})
    tier_overrides = cfg.get("tier_overrides", {})
    disabled = {k for k, v in tier_overrides.items()
                if isinstance(v, dict) and v.get("enabled") is False}

    out = {"generated_at": now.isoformat(), "windows": {}}

    # ---- 1. Per-instrument rolling 20-trade ----
    print("=" * 90)
    print(f"REGIME PULSE — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 90)
    print(f"\n[1] PER-INSTRUMENT ROLLING WINDOW (last {RECENT_WINDOW_TRADES} trades)")
    print("-" * 90)
    print(f"{'Instrument':10s} {'Strat':10s} {'n':>3s} {'WR':>6s} {'meanR':>8s} "
          f"{'meanW':>7s} {'meanL':>7s} {'last trade':>14s}  flags")

    per_ins = {}
    all_instruments = sorted(set(cfg_instruments))
    for ins in all_instruments:
        rows = db.execute(
            "SELECT r_multiple_gross, entry_ts FROM trades "
            "WHERE instrument = ? AND r_multiple_gross IS NOT NULL "
            "ORDER BY entry_ts DESC LIMIT ?",
            (ins, RECENT_WINDOW_TRADES)
        ).fetchall()
        rs = [r["r_multiple_gross"] for r in rows]
        s = _summarize(rs)
        last_ts = rows[0]["entry_ts"] if rows else None
        days_since = None
        if last_ts:
            try:
                dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                days_since = (now - dt).total_seconds() / 86400
            except Exception:
                pass

        strat = cfg_strat.get(ins, "pullback")
        if strat.startswith("_"):
            strat = "pullback"

        flags = []
        if ins in disabled: flags.append("DISABLED")
        if s["n"] < 5: flags.append("thin_data")
        if s["n"] >= 10 and s["mean_R"] < -0.1: flags.append("losing_regime")
        if s["n"] >= 10 and s["mean_R"] > 0.15: flags.append("hot")
        if days_since is not None and days_since > 7 and ins not in disabled:
            flags.append(f"dormant_{int(days_since)}d")

        meanR = f"{s['mean_R']:+.3f}R" if s["n"] else "n/a"
        wrs = f"{s['wr_pct']}%" if s["n"] else "n/a"
        mw = f"{s['mean_win_R']:+.2f}R" if s["n"] else "n/a"
        ml = f"{s['mean_loss_R']:+.2f}R" if s["n"] else "n/a"
        ds = f"{days_since:.1f}d" if days_since is not None else "never"

        print(f"{ins:10s} {strat:10s} {s['n']:>3d} {wrs:>6s} {meanR:>8s} "
              f"{mw:>7s} {ml:>7s} {ds:>14s}  {','.join(flags)}")

        per_ins[ins] = {
            "strategy": strat,
            "stats": s,
            "days_since_last_trade": round(days_since, 2) if days_since is not None else None,
            "disabled": ins in disabled,
            "flags": flags,
        }

    out["per_instrument"] = per_ins

    # ---- 2. Portfolio health: recent vs baseline ----
    print(f"\n[2] PORTFOLIO HEALTH  (recent={RECENT_WINDOW_DAYS}d  baseline={BASELINE_WINDOW_DAYS}d)")
    print("-" * 90)

    def window_stats(start_dt):
        rows = db.execute(
            "SELECT r_multiple_gross, profit_usd FROM trades "
            "WHERE entry_ts >= ? AND r_multiple_gross IS NOT NULL",
            (start_dt.isoformat(),)
        ).fetchall()
        rs = [r["r_multiple_gross"] for r in rows]
        pnl = sum((r["profit_usd"] or 0) for r in rows)
        s = _summarize(rs)
        s["pnl_usd"] = round(pnl, 2)
        return s

    recent = window_stats(now - timedelta(days=RECENT_WINDOW_DAYS))
    baseline = window_stats(now - timedelta(days=BASELINE_WINDOW_DAYS))
    print(f"{'metric':20s} {'recent':>12s} {'baseline':>12s} {'drift':>10s}")
    for key, pretty in [("n","trades"), ("wr_pct","WR %"),
                        ("mean_R","meanR"), ("rr","RR"),
                        ("mean_win_R","mean_win"), ("mean_loss_R","mean_loss"),
                        ("edge_vs_be","edge vs BE%"), ("pnl_usd","PnL $")]:
        rv = recent.get(key); bv = baseline.get(key)
        if rv is None or bv is None:
            print(f"  {pretty:18s} {'n/a':>12s} {'n/a':>12s}")
            continue
        drift = rv - bv
        flag = " <--" if (key == "mean_R" and drift < -0.10) or (key == "mean_loss_R" and drift < -0.15) else ""
        print(f"  {pretty:18s} {rv:>12} {bv:>12} {drift:>+10.3f}{flag}")

    out["portfolio_recent"] = recent
    out["portfolio_baseline"] = baseline

    # ---- 3. Outlier losses needing root-cause ----
    print(f"\n[3] OUTLIER LOSSES  (|R| > {OUTLIER_R}, last {RECENT_WINDOW_DAYS}d)")
    print("-" * 90)
    outlier_rows = db.execute(
        "SELECT entry_ts, exit_ts, instrument, side, outcome, r_multiple_gross, "
        "profit_usd, sl_initial, sl_at_exit, entry_price, exit_price "
        "FROM trades WHERE r_multiple_gross < ? AND entry_ts >= ? "
        "ORDER BY entry_ts DESC",
        (-OUTLIER_R, (now - timedelta(days=RECENT_WINDOW_DAYS)).isoformat())
    ).fetchall()
    outliers = []
    if not outlier_rows:
        print("  (none — mean loss tail is clean)")
    else:
        print(f"{'Instrument':10s} {'side':5s} {'R':>7s} {'$':>10s} {'outcome':16s} {'exit_ts':>22s}")
        for r in outlier_rows:
            print(f"{r['instrument']:10s} {r['side']:5s} {r['r_multiple_gross']:>+6.2f}R "
                  f"${r['profit_usd']:>+8.2f} {r['outcome']:16s} {r['exit_ts']:>22s}")
            outliers.append({
                "instrument": r["instrument"], "side": r["side"],
                "r": round(r["r_multiple_gross"], 2),
                "profit_usd": round(r["profit_usd"] or 0, 2),
                "outcome": r["outcome"], "exit_ts": r["exit_ts"],
            })
    out["outlier_losses"] = outliers

    # ---- 4. Signal frequency drift per strategy ----
    print(f"\n[4] SIGNAL FREQUENCY — trades per day per strategy")
    print("-" * 90)
    def freq_by_strat(start_dt, end_dt):
        rows = db.execute(
            "SELECT instrument, entry_ts FROM trades WHERE entry_ts >= ? AND entry_ts < ?",
            (start_dt.isoformat(), end_dt.isoformat())
        ).fetchall()
        days = (end_dt - start_dt).total_seconds() / 86400
        by_strat = Counter()
        for r in rows:
            s = cfg_strat.get(r["instrument"], "pullback")
            if s.startswith("_"):
                s = "pullback"
            by_strat[s] += 1
        return {k: v / days for k, v in by_strat.items()}, days

    f_recent, d_r = freq_by_strat(now - timedelta(days=RECENT_WINDOW_DAYS), now)
    f_base, d_b = freq_by_strat(now - timedelta(days=BASELINE_WINDOW_DAYS),
                                 now - timedelta(days=RECENT_WINDOW_DAYS))
    strats = sorted(set(list(f_recent.keys()) + list(f_base.keys())))
    print(f"  {'strategy':12s} {'recent/day':>12s} {'baseline/day':>14s} {'drift %':>10s}")
    for s in strats:
        r = f_recent.get(s, 0); b = f_base.get(s, 0)
        drift_pct = ((r - b) / b * 100) if b else None
        drift_s = f"{drift_pct:+.0f}%" if drift_pct is not None else "n/a"
        print(f"  {s:12s} {r:>12.2f} {b:>14.2f} {drift_s:>10s}")
    out["signal_frequency"] = {"recent_per_day": f_recent, "baseline_per_day": f_base}

    # ---- 5. Correlation coherence: same-day same-direction clusters ----
    print(f"\n[5] CORRELATION COHERENCE — multiple trades same instrument-group same day")
    print("-" * 90)
    GROUPS = {
        "USD_pairs": ["EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD"],
        "JPY_pairs": ["USDJPY", "EURJPY", "CHFJPY"],
        "US_indices": ["US500", "US100", "US30", "US2000"],
        "EU_indices": ["DE40", "UK100", "EU50"],
        "metals":    ["XAUUSD", "XAGUSD", "XPTUSD"],
        "oils":      ["USOIL", "UKOIL"],
        "crypto":    ["BTCUSD", "ETHUSD"],
    }
    rows = db.execute(
        "SELECT entry_ts, instrument, side FROM trades "
        "WHERE entry_ts >= ? ORDER BY entry_ts",
        ((now - timedelta(days=RECENT_WINDOW_DAYS)).isoformat(),)
    ).fetchall()
    by_date_group = defaultdict(lambda: defaultdict(list))  # date -> group -> [(ins,side)]
    for r in rows:
        date = r["entry_ts"][:10]
        for group, members in GROUPS.items():
            if r["instrument"] in members:
                by_date_group[date][group].append((r["instrument"], r["side"]))
    clusters = []
    for date in sorted(by_date_group.keys()):
        for group, items in by_date_group[date].items():
            if len(items) >= 2:
                sides = Counter(side for _, side in items)
                dominant = sides.most_common(1)[0]
                if dominant[1] >= 2:
                    cluster = {
                        "date": date, "group": group,
                        "n_trades": len(items),
                        "same_direction_n": dominant[1],
                        "direction": dominant[0],
                        "instruments": [i for i, _ in items],
                    }
                    clusters.append(cluster)
                    print(f"  {date}  {group:12s} n={len(items)}  "
                          f"{dominant[1]}x {dominant[0]}  "
                          f"{', '.join(i for i,_ in items)}")
    if not clusters:
        print("  (no correlation clusters in recent window)")
    out["correlation_clusters"] = clusters

    # ---- 6. Summary flags ----
    print(f"\n[6] HEADLINE FLAGS")
    print("-" * 90)
    headlines = []
    # Losing instruments
    losing = [i for i, d in per_ins.items() if "losing_regime" in d["flags"]]
    if losing:
        headlines.append(f"losing_regime: {', '.join(losing)}")
    hot = [i for i, d in per_ins.items() if "hot" in d["flags"]]
    if hot:
        headlines.append(f"hot: {', '.join(hot)}")
    dormant = [i for i, d in per_ins.items()
               if any(f.startswith("dormant") for f in d["flags"])]
    if dormant:
        headlines.append(f"dormant (>7d, enabled): {', '.join(dormant)}")
    if outliers:
        headlines.append(f"outlier losses in window: {len(outliers)}  (review each)")
    # Drift flags
    mean_R_drift = recent.get("mean_R", 0) - baseline.get("mean_R", 0)
    if mean_R_drift < -0.10:
        headlines.append(f"mean_R drift: {mean_R_drift:+.3f}R (recent much worse than baseline)")
    mean_loss_drift = recent.get("mean_loss_R", 0) - baseline.get("mean_loss_R", 0)
    if mean_loss_drift < -0.15:
        headlines.append(f"mean_loss blown: {mean_loss_drift:+.3f}R (loss tail widening)")
    if not headlines:
        headlines.append("no critical flags")
    for h in headlines:
        print(f"  • {h}")
    out["headline_flags"] = headlines

    # ---- Persist ----
    with open("regime_pulse.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    os.makedirs("reports/regime_pulse", exist_ok=True)
    date_tag = now.strftime("%Y%m%d")
    with open(f"reports/regime_pulse/{date_tag}.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"\n[saved] regime_pulse.json + reports/regime_pulse/{date_tag}.json")


if __name__ == "__main__":
    main()
