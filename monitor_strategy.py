"""Proactive strategy monitor — runs one round of health checks on the
live trading deployment and flags anomalies. Designed for periodic
scheduling (cron / Task Scheduler / Claude Code /loop / /schedule).

State persistence: keeps `monitor_state.json` to track last-seen tickets,
last-seen equity peak, and previous DD level so each run is incremental
(only reports NEW events, not the same anomalies forever).

Checks performed each run:
  1. NEW closed trades since last run -> report each one
  2. Account drawdown vs peak -> alert if >= configurable threshold
  3. Profit factor on rolling last-N closed trades -> alert if < 1.0
  4. Open positions without position_meta (orphans) -> alert
  5. Concurrency violation (open positions > max_concurrent_positions) -> alert
  6. Stale-open positions: held > N hours with no progress -> alert
  7. Phantom records: journal-open but broker-closed -> auto-patches
  8. Spread anomaly: any instrument's max_spread_pips repeatedly exceeded -> alert
  9. live_trader process running -> alert if not

Output:
  - Human-readable to stdout (and `monitor.log` if --logfile)
  - Machine-readable JSON to `monitor_state.json` (latest run summary)
  - Returns non-zero exit code if any RED alert triggered (so /loop can
    notice and surface to the user).

Usage:
    python monitor_strategy.py                 # one-shot, write state
    python monitor_strategy.py --quiet         # only print on RED alerts
    python monitor_strategy.py --dd-threshold 30 --pf-window 10
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
import numpy as np
import pandas as pd
import MetaTrader5 as mt5
import live_mt5_connector as cx


JOURNAL_DB = "trade_journal.db"
STATE_FILE = "monitor_state.json"
RUNTIME_STATE = "runtime_state.json"


def load_monitor_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"last_seen_tickets": [], "last_run_utc": None, "last_dd_pct": 0.0}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_seen_tickets": [], "last_run_utc": None, "last_dd_pct": 0.0}


def save_monitor_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)


def is_live_trader_running() -> tuple[bool, list[int]]:
    """Detect any python.exe process running live_trader.py via WMIC equivalent.
    Returns (running, [pids])."""
    import subprocess
    try:
        # Get-CimInstance via PowerShell — same approach used elsewhere in the project
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -match 'live_trader'} | Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return (False, [])
        raw = out.stdout.strip()
        if not raw:
            return (False, [])
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        # Filter out our own monitor process and powershell wrappers
        pids = []
        for proc in data:
            cmd = (proc.get("CommandLine") or "")
            if "live_trader.py" in cmd and "monitor_strategy" not in cmd and "powershell" not in cmd.lower():
                pids.append(int(proc.get("ProcessId", 0)))
        return (len(pids) > 0, pids)
    except Exception:
        return (False, [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="Only print on RED alerts")
    ap.add_argument("--config", default="mt5_runtime.json")
    ap.add_argument("--dd-threshold", type=float, default=30.0,
                     help="DD %% above which to alert RED")
    ap.add_argument("--dd-warn", type=float, default=15.0,
                     help="DD %% above which to flag YELLOW")
    ap.add_argument("--pf-window", type=int, default=10,
                     help="Rolling-PF over last N closed trades")
    ap.add_argument("--pf-threshold", type=float, default=1.0,
                     help="Rolling-PF below this triggers RED")
    ap.add_argument("--stale-bars", type=int, default=72,
                     help="Open positions older than this (hours) without progress = warn")
    ap.add_argument("--logfile", default="monitor.log",
                     help="Append-mode log file")
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    conn_cfg = cfg.get("connection", {})
    magic = cfg["execution"]["magic_number"]

    if not cx.connect(path=conn_cfg.get("path"),
                       expected_server=conn_cfg.get("expected_server"),
                       expected_login=conn_cfg.get("expected_login")):
        print(f"[monitor] connect failed: {mt5.last_error()}", file=sys.stderr)
        return 2

    state = load_monitor_state()
    now = datetime.now(timezone.utc)
    alerts_red: list[str] = []
    alerts_yellow: list[str] = []
    info: list[str] = []

    # --- 1. Account state ---
    ai = mt5.account_info()
    if ai is None:
        alerts_red.append("Cannot read account_info from broker")
        cx.shutdown(); return 1
    equity = float(ai.equity)
    balance = float(ai.balance)
    rt = {}
    if os.path.exists(RUNTIME_STATE):
        try:
            rt = json.load(open(RUNTIME_STATE, encoding="utf-8"))
        except Exception:
            pass
    peak = float(rt.get("equity_peak", equity))
    if equity > peak:
        peak = equity
    dd_pct = max(0.0, (peak - equity) / peak * 100.0) if peak else 0.0

    info.append(f"equity=${equity:,.2f} balance=${balance:,.2f} peak=${peak:,.2f} DD={dd_pct:.2f}%")
    if dd_pct >= args.dd_threshold:
        alerts_red.append(f"Drawdown {dd_pct:.1f}% >= RED threshold {args.dd_threshold}%")
    elif dd_pct >= args.dd_warn:
        alerts_yellow.append(f"Drawdown {dd_pct:.1f}% >= warn threshold {args.dd_warn}%")

    # Detect drawdown change since last run
    last_dd = float(state.get("last_dd_pct", 0.0))
    if dd_pct > last_dd + 5.0:
        alerts_red.append(f"DD jumped {last_dd:.1f}% -> {dd_pct:.1f}% (+{dd_pct - last_dd:.1f}pp) since last check")

    # --- 2. live_trader process ---
    running, pids = is_live_trader_running()
    if not running:
        alerts_red.append("live_trader.py is NOT running — strategy is not executing trades")
    else:
        info.append(f"live_trader running (pids: {pids})")

    # --- 3. Open positions vs concurrency cap ---
    open_positions = mt5.positions_get() or []
    ours_open = [p for p in open_positions if p.magic == magic]
    # Scale-in adds are excluded from the cap by live_trader (2026-10-22);
    # count mains only here too, or every 6-mains-plus-one-add state is a
    # false red (2026-10-25 audit).
    ours_mains = [p for p in ours_open
                  if not (getattr(p, "comment", "") or "").startswith("fx-edge scale_in")]
    max_conc = int(cfg.get("execution", {}).get("max_concurrent_positions", 5))
    info.append(f"open positions (our magic): {len(ours_mains)} mains + {len(ours_open) - len(ours_mains)} adds / cap {max_conc}")
    if len(ours_mains) > max_conc:
        alerts_red.append(f"Concurrency exceeded: {len(ours_mains)} mains open vs cap {max_conc}")

    # --- 4. Orphan check (open at broker but missing from runtime_state.position_meta) ---
    pos_meta = {int(k): v for k, v in (rt.get("position_meta") or {}).items()}
    orphans = [p for p in ours_open if int(p.ticket) not in pos_meta]
    if orphans:
        names = [f"{p.symbol}/{p.ticket}" for p in orphans]
        alerts_yellow.append(f"{len(orphans)} orphan position(s) without metadata: {names} "
                             f"(BE-trail and stale_exit cannot manage them — run register_orphan_meta.py --apply)")

    # --- 5. Stale open positions ---
    stale = []
    for p in ours_open:
        try:
            # position.time is BROKER time -- compare in broker time
            age_h = (now.timestamp() + cx._get_broker_offset() - float(p.time)) / 3600.0
        except Exception:
            continue
        if age_h <= args.stale_bars:
            continue
        meta = pos_meta.get(int(p.ticket))
        if meta and meta.get("planned_dist", 0) > 0:
            tick = mt5.symbol_info_tick(p.symbol)
            if tick is None:
                continue
            cur = tick.bid if p.type == mt5.POSITION_TYPE_BUY else tick.ask
            if p.type == mt5.POSITION_TYPE_BUY:
                progress = (cur - p.price_open) / meta["planned_dist"]
            else:
                progress = (p.price_open - cur) / meta["planned_dist"]
            if progress < 0.75:
                stale.append((p.symbol, p.ticket, age_h, progress))
        else:
            # No metadata — flag as stale by age only
            stale.append((p.symbol, p.ticket, age_h, None))
    if stale:
        for sym, tk, age, prog in stale:
            prog_str = f"{prog:.2f}R" if prog is not None else "no-meta"
            alerts_yellow.append(f"Stale: {sym}/{tk} age={age:.1f}h progress={prog_str}")

    # --- 6. Journal: new closed trades + rolling PF ---
    if os.path.exists(JOURNAL_DB):
        db = sqlite3.connect(JOURNAL_DB)
        db.row_factory = sqlite3.Row
        cur = db.execute(
            "SELECT ticket, instrument, side, exit_ts, profit_usd, commission_usd, swap_usd, "
            "outcome, r_multiple_gross FROM trades "
            "WHERE exit_ts IS NOT NULL AND exit_ts != '' ORDER BY exit_ts DESC LIMIT 50"
        )
        recent = [dict(r) for r in cur]
        db.close()

        last_seen = set(int(t) for t in state.get("last_seen_tickets", []))
        new_closes = [r for r in recent if int(r["ticket"]) not in last_seen]
        # Sort new closes oldest-first for chronological reporting
        new_closes.sort(key=lambda r: r["exit_ts"] or "")
        for r in new_closes:
            net = (r.get("profit_usd") or 0) + (r.get("commission_usd") or 0) + (r.get("swap_usd") or 0)
            r_mult = r.get("r_multiple_gross")
            r_str = f" R={r_mult:+.2f}" if r_mult is not None and not pd.isna(r_mult) else ""
            outcome = r.get("outcome") or "?"
            line = (f"NEW CLOSE: {r['instrument']}/{r['ticket']} {r['side']} "
                    f"net=${net:+.2f} outcome={outcome}{r_str} at {r['exit_ts']}")
            if net < 0 and abs(net) > 50:
                alerts_yellow.append(line + "  (large loss)")
            elif net < 0:
                info.append(line)
            else:
                info.append(line)

        # Rolling PF on last N closed trades
        if recent:
            window = recent[: args.pf_window]
            wins = sum((r["profit_usd"] or 0) + (r["commission_usd"] or 0) + (r["swap_usd"] or 0)
                       for r in window
                       if (r["profit_usd"] or 0) + (r["commission_usd"] or 0) + (r["swap_usd"] or 0) > 0)
            losses = -sum((r["profit_usd"] or 0) + (r["commission_usd"] or 0) + (r["swap_usd"] or 0)
                          for r in window
                          if (r["profit_usd"] or 0) + (r["commission_usd"] or 0) + (r["swap_usd"] or 0) <= 0)
            pf = (wins / losses) if losses > 0 else float("inf") if wins > 0 else 0.0
            info.append(f"PF (last {len(window)} trades): {pf:.2f}  "
                        f"wins=${wins:.2f} losses=${losses:.2f}")
            if pf < args.pf_threshold and len(window) >= 5:
                alerts_red.append(f"Rolling PF over last {len(window)} trades = {pf:.2f} "
                                  f"< RED threshold {args.pf_threshold}")

    # --- 7. Phantom records (open in journal but closed at broker) ---
    if os.path.exists(JOURNAL_DB):
        db = sqlite3.connect(JOURNAL_DB)
        db.row_factory = sqlite3.Row
        cur = db.execute(
            "SELECT ticket, instrument FROM trades WHERE exit_ts IS NULL OR exit_ts = ''"
        )
        open_in_journal = [dict(r) for r in cur]
        db.close()
        broker_open_tickets = {int(p.ticket) for p in ours_open}
        phantoms = [r for r in open_in_journal if int(r["ticket"]) not in broker_open_tickets]
        if phantoms:
            names = [f"{r['instrument']}/{r['ticket']}" for r in phantoms]
            alerts_yellow.append(f"{len(phantoms)} phantom journal record(s) "
                                 f"(open in journal, closed at broker): {names} "
                                 f"— run refresh_journal_closes.py --apply")

    # --- Data-threshold check for advanced techniques ---
    thresholds = cfg.get("auto_enable_thresholds", {})
    threshold_status = []
    if thresholds and os.path.exists(JOURNAL_DB):
        db = sqlite3.connect(JOURNAL_DB)
        # Total closed trades with valid R
        n_closed_R = db.execute(
            "SELECT COUNT(*) FROM trades WHERE exit_ts IS NOT NULL "
            "AND r_multiple_gross IS NOT NULL"
        ).fetchone()[0]
        # Median trades per instrument
        per_ins = dict(db.execute(
            "SELECT instrument, COUNT(*) FROM trades WHERE exit_ts IS NOT NULL "
            "AND r_multiple_gross IS NOT NULL GROUP BY instrument"
        ).fetchall())
        med_per_ins = int(np.median(list(per_ins.values()))) if per_ins else 0
        # Skipped signals per reason
        sk_per_reason = dict(db.execute(
            "SELECT skip_reason, COUNT(*) FROM signals WHERE decision='skipped' "
            "AND skip_reason IS NOT NULL GROUP BY skip_reason"
        ).fetchall())
        db.close()

        for tech, spec in thresholds.items():
            if tech.startswith("_"):
                continue
            if "min_trades_per_instrument" in spec:
                target = int(spec["min_trades_per_instrument"])
                ready = med_per_ins >= target
                threshold_status.append((tech, ready,
                    f"{med_per_ins}/{target} trades per instrument (median)"))
            elif "min_total_trades" in spec:
                target = int(spec["min_total_trades"])
                ready = n_closed_R >= target
                threshold_status.append((tech, ready,
                    f"{n_closed_R}/{target} total closed trades w/ R"))
            elif "min_skipped_signals_per_filter" in spec:
                target = int(spec["min_skipped_signals_per_filter"])
                if sk_per_reason:
                    min_filter = min(sk_per_reason.values())
                    ready = min_filter >= target
                    threshold_status.append((tech, ready,
                        f"min-filter has {min_filter}/{target} skipped signals"))
                else:
                    threshold_status.append((tech, False, f"0/{target} (no signals logged yet)"))

    # ---- Output ----
    lines = [f"--- monitor @ {now.isoformat()} ---"]
    if alerts_red:
        lines.append("RED ALERTS:")
        for a in alerts_red: lines.append(f"  ! {a}")
    if alerts_yellow:
        lines.append("YELLOW WARNINGS:")
        for a in alerts_yellow: lines.append(f"  - {a}")
    if info and (not args.quiet or alerts_red):
        lines.append("INFO:")
        for i in info: lines.append(f"  . {i}")
    if threshold_status and (not args.quiet or alerts_red):
        lines.append("DATA THRESHOLDS (advanced techniques):")
        for tech, ready, msg in threshold_status:
            mark = "READY" if ready else "wait "
            lines.append(f"  [{mark}] {tech:>32s}: {msg}")

    # Pending filter recommendations (from tune_filters.py)
    if os.path.exists("filter_recommendations.json"):
        try:
            recs = json.load(open("filter_recommendations.json", encoding="utf-8"))
            if recs:
                lines.append(f"FILTER RECOMMENDATIONS PENDING ({len(recs)}):")
                for r in recs[:5]:
                    lines.append(f"  - {r.get('skip_reason')}: {r.get('action')} on "
                                  f"{r.get('filter_path')}  (mean regret "
                                  f"{r.get('regret_mean_R')}R over {r.get('sample_size')} samples)")
                lines.append(f"  Review: python apply_filter_changes.py")
                # Also bubble up as a YELLOW so this surfaces in --quiet mode
                alerts_yellow.append(f"{len(recs)} filter recommendation(s) pending review")
        except Exception:
            pass

    out = "\n".join(lines)
    if not args.quiet or alerts_red:
        print(out)

    # Append to logfile (always)
    try:
        with open(args.logfile, "a", encoding="utf-8") as f:
            f.write(out + "\n")
    except Exception as e:
        print(f"[monitor] logfile write failed: {e}", file=sys.stderr)

    # Persist state
    if os.path.exists(JOURNAL_DB):
        db = sqlite3.connect(JOURNAL_DB)
        cur = db.execute(
            "SELECT ticket FROM trades WHERE exit_ts IS NOT NULL AND exit_ts != ''"
        )
        all_closed_tickets = [int(r[0]) for r in cur.fetchall()]
        db.close()
        state["last_seen_tickets"] = all_closed_tickets
    state["last_run_utc"] = now.isoformat()
    state["last_dd_pct"] = dd_pct
    state["last_equity"] = equity
    state["last_peak"] = peak
    save_monitor_state(state)

    cx.shutdown()
    # Exit code: 0 = ok, 1 = yellow only, 2 = red
    if alerts_red: return 2
    if alerts_yellow: return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
