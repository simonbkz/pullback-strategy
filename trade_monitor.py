"""Live trade monitor (complement to watchdog.py).

watchdog.py    -- watches the live_trader PROCESS (alive? saving state?)
trade_monitor  -- watches the TRADES (entries, exits, P&L, mechanisms firing)

Polls every N seconds and reports:
  1. Open positions with live unrealized R-multiple + age + which protection
     mechanisms have fired (BE armed, chandelier armed, in delay window).
  2. Account state (equity, peak, drawdown%, banked, consecutive losses).
  3. New events since last poll: trades opened/closed, protections triggered.
  4. Today's running P&L and trade outcomes.
  5. Active alert flags (consec losses >= threshold, DD >= 30%, etc.).

Writes a JSON status snapshot to monitor_status.json (consumable by other
tools or a dashboard). Sends a console alert line whenever a meaningful
event happens.

Run with:
    .\\.venv\\Scripts\\python.exe trade_monitor.py
    .\\.venv\\Scripts\\python.exe trade_monitor.py --interval 30 --quiet
"""
from __future__ import annotations
import argparse, json, os, sqlite3, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5
import live_mt5_connector as cx


def utc_now():
    return datetime.now(timezone.utc)


def load_state_file():
    p = Path("runtime_state.json")
    if not p.exists(): return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def fetch_open_positions(cfg, runtime_state):
    """Return list of dicts describing each open position with our magic."""
    magic = cfg["execution"]["magic_number"]
    positions = mt5.positions_get() or []
    out = []
    pos_meta = runtime_state.get("position_meta", {})
    be_armed_set = set(runtime_state.get("be_armed", []))
    pending_swap = runtime_state.get("pending_sl_swap", {})

    for p in positions:
        if p.magic != magic: continue
        ticket = int(p.ticket)
        meta = pos_meta.get(str(ticket), {})
        side = meta.get("side") or ("long" if p.type == mt5.POSITION_TYPE_BUY else "short")
        entry = float(meta.get("entry_price", p.price_open))
        planned_dist = float(meta.get("planned_dist", abs(p.price_open - p.sl)))
        # Live R-multiple
        tick = mt5.symbol_info_tick(p.symbol)
        cur_price = (tick.bid if side == "long" else tick.ask) if tick else None
        unrealized_R = None
        if cur_price is not None and planned_dist > 0:
            if side == "long":
                unrealized_R = (cur_price - entry) / planned_dist
            else:
                unrealized_R = (entry - cur_price) / planned_dist
        # Bars elapsed via MT5 (most reliable -- matches the trader's
        # _h1_bars_since used by decay_sl + stale_exit). HFM reports p.time
        # in broker-local seconds which can be ahead of UTC, so we don't
        # trust raw age_hours.
        try:
            entry_dt = datetime.fromtimestamp(p.time, tz=timezone.utc)
            rates = mt5.copy_rates_range(p.symbol, mt5.TIMEFRAME_H1, entry_dt, utc_now())
            bars_elapsed = max(0, (len(rates) - 1) if rates is not None else 0)
        except Exception:
            bars_elapsed = 0
        # Approximate age = bars * 3600s (only used for display)
        age_hours = bars_elapsed * 1.0

        in_delay = str(ticket) in pending_swap and time.time() < pending_swap[str(ticket)].get("swap_at_unix", 0)

        flags = []
        if in_delay: flags.append("DELAY")
        if ticket in be_armed_set: flags.append("BE_OR_CHAND")

        out.append({
            "ticket": ticket,
            "symbol": p.symbol,
            "ins": meta.get("ins", p.symbol),
            "side": side,
            "volume": float(p.volume),
            "entry": entry,
            "sl": float(p.sl),
            "tp": float(p.tp),
            "current": cur_price,
            "unrealized_usd": float(p.profit),
            "unrealized_R": unrealized_R,
            "age_hours": round(age_hours, 1),
            "bars_elapsed": bars_elapsed,
            "flags": flags,
        })
    return out


def fetch_journal_summary(conn, cur_magic):
    """Compute today's trade stats and recent close events."""
    today_utc = utc_now().strftime("%Y-%m-%d")
    cutoff_recent = (utc_now() - timedelta(hours=24)).isoformat()

    cur = conn.execute(
        "SELECT ticket, instrument, side, outcome, r_multiple_gross, profit_usd, "
        "exit_ts, be_armed, decay_tighten FROM trades "
        "WHERE exit_ts IS NOT NULL AND exit_ts > ? ORDER BY exit_ts DESC LIMIT 50",
        (cutoff_recent,),
    )
    recent_closes = []
    today_pnl = 0.0
    today_wins = 0
    today_losses = 0
    today_n = 0
    for r in cur.fetchall():
        rec = {
            "ticket": r[0], "instrument": r[1], "side": r[2],
            "outcome": r[3], "r": r[4], "profit_usd": r[5],
            "exit_ts": r[6], "be_armed": bool(r[7]),
            "decay_tighten": bool(r[8] or 0),
        }
        recent_closes.append(rec)
        if (r[6] or "").startswith(today_utc):
            today_n += 1
            today_pnl += float(r[5] or 0)
            if (r[5] or 0) > 0: today_wins += 1
            elif (r[5] or 0) < 0: today_losses += 1

    return {
        "today_n": today_n, "today_pnl": today_pnl,
        "today_wins": today_wins, "today_losses": today_losses,
        "recent_closes": recent_closes,
    }


def compute_alerts(cfg, account, positions, summary, runtime):
    alerts = []
    eq = account["equity"]
    peak = max(account["equity"], float(runtime.get("equity_peak", eq)))
    dd_pct = ((peak - eq) / peak * 100) if peak > 0 else 0

    # DD warnings
    if dd_pct >= 30:
        alerts.append(f"DD {dd_pct:.1f}% from peak ${peak:,.2f}")
    cb = cfg.get("dd_circuit_breaker", {})
    if cb.get("enabled") and dd_pct >= cb.get("throttle_pct", 40):
        alerts.append(f"DD circuit breaker THROTTLE active (DD {dd_pct:.1f}% >= {cb['throttle_pct']}%)")
    if cb.get("enabled") and dd_pct >= cb.get("pause_pct", 60):
        alerts.append(f"DD circuit breaker PAUSE: all new entries blocked")

    # Loss streak
    consec = int(runtime.get("consecutive_losses", 0))
    lst = cfg.get("loss_streak_throttle", {})
    if lst.get("enabled") and consec >= lst.get("threshold", 3):
        alerts.append(f"loss_streak_throttle active ({consec} consecutive losses)")
    elif consec >= 2:
        alerts.append(f"approaching loss-streak threshold ({consec} consecutive)")

    # Open trade alerts
    for p in positions:
        if p["unrealized_R"] is not None:
            if p["unrealized_R"] >= 2.0 and "BE_OR_CHAND" not in p["flags"]:
                alerts.append(f"{p['ins']} #{p['ticket']} at +{p['unrealized_R']:.2f}R but no BE/chandelier flag yet")
            if p["unrealized_R"] <= -0.9:
                alerts.append(f"{p['ins']} #{p['ticket']} at {p['unrealized_R']:.2f}R, near SL")

    # Margin
    if account.get("margin_used", 0) > 0 and account.get("equity", 0) > 0:
        ml = account["equity"] / max(account["margin_used"], 0.01) * 100
        if ml < 200:
            alerts.append(f"margin level {ml:.0f}% (broker stop-out near 50%)")

    return alerts, dd_pct


def render_dashboard(account, positions, summary, alerts, dd_pct, runtime):
    lines = []
    lines.append("=" * 90)
    now = utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
    lines.append(f" trade_monitor @ {now}")
    lines.append("=" * 90)

    # Account
    eq = account["equity"]; bal = account["balance"]
    peak = max(eq, float(runtime.get("equity_peak", eq)))
    banked = float(runtime.get("banked_equity", 0))
    start = float(runtime.get("start_equity", 0))
    consec = int(runtime.get("consecutive_losses", 0))
    lines.append(f" ACCOUNT  equity=${eq:,.2f}  balance=${bal:,.2f}  peak=${peak:,.2f}  DD={dd_pct:.1f}%")
    lines.append(f"          margin=${account.get('margin_used', 0):,.2f}  free=${account.get('margin_free', 0):,.2f}")
    lines.append(f"          ratchet: start=${start:,.2f}  banked=${banked:,.2f}  consec_losses={consec}")

    # Open positions
    lines.append("")
    if positions:
        lines.append(f" OPEN POSITIONS ({len(positions)})")
        lines.append(f"   {'ticket':>10} {'ins':<8} {'side':<5} {'vol':>6} {'R':>7} {'$':>9} {'bars':>5} {'flags':<15}")
        for p in positions:
            r_str = f"{p['unrealized_R']:+.2f}" if p["unrealized_R"] is not None else "-"
            flag_str = ",".join(p["flags"]) or "-"
            lines.append(f"   {p['ticket']:>10} {p['ins']:<8} {p['side']:<5} {p['volume']:>6.2f} "
                          f"{r_str:>7} {p['unrealized_usd']:>+9.2f} {p['bars_elapsed']:>5} {flag_str:<15}")
    else:
        lines.append(" OPEN POSITIONS  (none)")

    # Today's stats
    lines.append("")
    lines.append(f" TODAY  trades={summary['today_n']}  wins={summary['today_wins']}  losses={summary['today_losses']}  "
                  f"P&L=${summary['today_pnl']:+,.2f}")

    # Recent closes
    if summary["recent_closes"]:
        lines.append("")
        lines.append(" RECENT CLOSES (last 24h)")
        for c in summary["recent_closes"][:8]:
            r_str = f"{c['r']:+.2f}R" if c["r"] is not None else "-"
            tags = []
            if c["be_armed"]: tags.append("BE")
            if c["decay_tighten"]: tags.append("decay")
            tag_s = "[" + ",".join(tags) + "]" if tags else ""
            lines.append(f"   {c['exit_ts'][:19]}  {c['instrument']:<8} {c['side']:<5} "
                          f"{c['outcome']:<14} {r_str:>7} ${c['profit_usd']:>+8.2f} {tag_s}")

    # Alerts
    if alerts:
        lines.append("")
        lines.append(" ALERTS")
        for a in alerts:
            lines.append(f"   * {a}")
    return "\n".join(lines)


def write_status_json(out_path, account, positions, summary, alerts, dd_pct, runtime):
    payload = {
        "ts": utc_now().isoformat(),
        "account": account,
        "dd_pct": round(dd_pct, 2),
        "consecutive_losses": int(runtime.get("consecutive_losses", 0)),
        "banked_equity": float(runtime.get("banked_equity", 0)),
        "start_equity": float(runtime.get("start_equity", 0)),
        "positions": positions,
        "today": {
            "n": summary["today_n"], "pnl": summary["today_pnl"],
            "wins": summary["today_wins"], "losses": summary["today_losses"],
        },
        "recent_closes": summary["recent_closes"][:20],
        "alerts": alerts,
    }
    try:
        Path(out_path).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="mt5_runtime.json")
    ap.add_argument("--interval", type=int, default=60, help="poll interval (seconds)")
    ap.add_argument("--quiet", action="store_true", help="only print on event change / alert")
    ap.add_argument("--status-out", default="monitor_status.json", help="JSON status snapshot path")
    ap.add_argument("--once", action="store_true", help="single poll then exit (useful for cron/scripts)")
    args = ap.parse_args()

    cfg = json.load(open(args.config, "r", encoding="utf-8"))
    conn_cfg = cfg.get("connection", {})
    if not cx.connect(path=conn_cfg.get("path"),
                       expected_server=conn_cfg.get("expected_server"),
                       expected_login=conn_cfg.get("expected_login")):
        print("[trade_monitor] MT5 connect failed")
        return 1

    journal_db = os.environ.get("FXEDGE_JOURNAL_DB", "trade_journal.db")
    print(f"[trade_monitor] starting (interval={args.interval}s, db={journal_db}, status_out={args.status_out})")

    prev_open_tickets = set()
    prev_close_count = 0
    prev_consec = 0

    try:
        while True:
            try:
                runtime = load_state_file()
                ai = mt5.account_info()
                account = {
                    "login": ai.login, "server": ai.server,
                    "equity": float(ai.equity), "balance": float(ai.balance),
                    "margin_used": float(ai.margin), "margin_free": float(ai.margin_free),
                    "leverage": int(ai.leverage),
                }
                positions = fetch_open_positions(cfg, runtime)
                conn = sqlite3.connect(journal_db)
                summary = fetch_journal_summary(conn, cfg["execution"]["magic_number"])
                conn.close()
                alerts, dd_pct = compute_alerts(cfg, account, positions, summary, runtime)

                # Event detection
                cur_tickets = {p["ticket"] for p in positions}
                opened = cur_tickets - prev_open_tickets
                closed_diff = max(0, len(summary["recent_closes"]) - prev_close_count) if summary["recent_closes"] else 0
                consec_changed = int(runtime.get("consecutive_losses", 0)) != prev_consec

                has_event = bool(opened or closed_diff > 0 or consec_changed or alerts)

                if not args.quiet or has_event:
                    dash = render_dashboard(account, positions, summary, alerts, dd_pct, runtime)
                    print(dash)
                    print()

                if opened:
                    for ticket in opened:
                        match = next((p for p in positions if p["ticket"] == ticket), None)
                        if match:
                            print(f"   >>> NEW TRADE: {match['ins']} {match['side']} vol={match['volume']:.2f} "
                                  f"entry={match['entry']} SL={match['sl']} TP={match['tp']}")

                if closed_diff > 0:
                    for c in summary["recent_closes"][:closed_diff]:
                        sign = "WIN" if (c["profit_usd"] or 0) > 0 else "LOSS"
                        r_val = c["r"] if c["r"] is not None else 0.0
                        print(f"   <<< CLOSED ({sign}): {c['instrument']} {c['outcome']} "
                              f"r={r_val:+.2f}R ${c['profit_usd']:+.2f}")

                write_status_json(args.status_out, account, positions, summary, alerts, dd_pct, runtime)

                prev_open_tickets = cur_tickets
                prev_close_count = len(summary["recent_closes"])
                prev_consec = int(runtime.get("consecutive_losses", 0))

            except Exception as e:
                print(f"[trade_monitor] tick error: {e}")
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[trade_monitor] stopped")
    finally:
        cx.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
