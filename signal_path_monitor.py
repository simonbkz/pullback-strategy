"""Expected-vs-observed signal monitor — catches BROKEN PATHS, not quiet markets.

For every enabled instrument: run the live strategy on the last N days of live
MT5 bars and count how many entry signals it *would* have produced. Compare to
how many signal rows (any decision) actually reached the journal. If the code
would have fired and nothing was logged, the path between bars and _log_signal
is broken (symbol disabled, market-open gate, bar-fetch short, etc.) — exactly
the failure mode that hid 13 FX pairs for months (2026-10-20).

Writes reports/signal_path/<date>.json and prints a headline. Exit code 2 on
any ALERT so the scheduled task's LastTaskResult surfaces it.
"""
import sys, json, sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
sys.path.insert(0, str(Path(__file__).resolve().parent))
import MetaTrader5 as mt5
import live_mt5_connector as cx
import live_trader as lt
from config import DEFAULT_PARAMS

LOOKBACK_DAYS = 7
MIN_EXPECTED_TO_JUDGE = 4      # below this, silence is plausibly just quiet
ALERT_RATIO = 0.0              # observed/expected == 0 with expected>=MIN -> ALERT
WARN_RATIO = 0.35              # observed < 35% of expected -> WARN

cfg = json.load(open("mt5_runtime.json", encoding="utf-8"))
db = sqlite3.connect("trade_journal.db")
now = datetime.now(timezone.utc)
since = (now - timedelta(days=LOOKBACK_DAYS)).isoformat()
enabled = [i for i in cfg["instruments"]
           if not (isinstance(cfg["tier_overrides"].get(i), dict) and cfg["tier_overrides"][i].get("enabled") is False)]

if not mt5.initialize():
    print("mt5 init fail"); sys.exit(1)

rows = []
for ins in enabled:
    broker = cx.resolve_symbol(ins, cfg.get("symbol_map"))
    rec = {"instrument": ins, "broker": broker, "expected": None, "observed": None, "status": "?", "note": ""}
    if broker is None:
        rec["status"] = "UNRESOLVED"; rows.append(rec); continue
    info = mt5.symbol_info(broker)
    if info is not None and info.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
        rec["status"] = "ALERT"; rec["note"] = "resolved symbol is trade-disabled"; rows.append(rec); continue
    try:
        ltf = cx.fetch_bars(broker, mt5.TIMEFRAME_H1, 1500)
        htf = cx.fetch_bars(broker, mt5.TIMEFRAME_H4, 600)
    except Exception as e:
        rec["status"] = "ALERT"; rec["note"] = f"fetch_bars error: {str(e)[:60]}"; rows.append(rec); continue
    if len(ltf) < 250 or len(htf) < 60:
        rec["status"] = "ALERT"; rec["note"] = f"bars short H1={len(ltf)} H4={len(htf)}"; rows.append(rec); continue
    try:
        sig = lt._get_entry_signal(cfg, ins, ltf, htf, DEFAULT_PARAMS)
        cutoff = ltf.index[-1] - timedelta(days=LOOKBACK_DAYS)
        expected = int(sig.loc[sig.index >= cutoff, "side"].isin(["long", "short"]).sum())
    except Exception as e:
        rec["status"] = "ALERT"; rec["note"] = f"signal gen error: {str(e)[:60]}"; rows.append(rec); continue
    observed = db.execute("SELECT COUNT(*) FROM signals WHERE instrument=? AND ts>=?", (ins, since)).fetchone()[0]
    rec["expected"] = expected; rec["observed"] = observed
    if expected < MIN_EXPECTED_TO_JUDGE:
        rec["status"] = "ok"; rec["note"] = "too few expected to judge"
    elif observed == 0:
        rec["status"] = "ALERT"; rec["note"] = "strategy fires but NOTHING reached the journal — path broken"
    elif observed < WARN_RATIO * expected:
        rec["status"] = "WARN"; rec["note"] = f"observed {observed} vs expected {expected}"
    else:
        rec["status"] = "ok"
    rows.append(rec)
# ---------------------------------------------------------------------------
# EXIT-SIDE reconciliation (added 2026-10-21 after the lost-closes bug).
# The entry-side check above cannot see closes that never reached the journal.
# Compare broker OUT deals for our magic in the window vs journal exits.
#   phantom_open : journal says open, broker has an OUT deal   -> ALERT
#   unjournaled  : broker OUT deal (our magic), no journal row  -> ALERT
# ---------------------------------------------------------------------------
magic = int(cfg.get("execution", {}).get("magic_number") or cfg.get("magic_number") or 0)
if not magic:
    print("WARN: magic_number not found in config — exit reconciliation will see nothing")
exit_rows = []
try:
    deals = mt5.history_deals_get(now - timedelta(days=LOOKBACK_DAYS), now + timedelta(hours=6)) or []
    outs = {int(d.position_id): d for d in deals if d.magic == magic and d.entry == mt5.DEAL_ENTRY_OUT}
    open_now = {int(p.ticket) for p in (mt5.positions_get() or []) if p.magic == magic}
    j_open = {int(r[0]) for r in db.execute("SELECT ticket FROM trades WHERE exit_ts IS NULL")}
    j_known = {int(r[0]) for r in db.execute("SELECT ticket FROM trades")}
    for t in sorted(j_open - open_now):
        if t in outs:
            exit_rows.append({"ticket": t, "kind": "phantom_open", "status": "ALERT",
                              "note": f"journal open, broker closed {datetime.fromtimestamp(outs[t].time, timezone.utc):%m-%d %H:%M} ${outs[t].profit:+.2f}"})
    for t in sorted(set(outs) - j_known):
        exit_rows.append({"ticket": t, "kind": "unjournaled", "status": "ALERT",
                          "note": f"{outs[t].symbol} closed ${outs[t].profit:+.2f}, no journal row at all"})
    exit_summary = {"broker_outs": len(outs), "journal_open": len(j_open), "broker_open": len(open_now), "issues": len(exit_rows)}
except Exception as e:
    exit_summary = {"error": str(e)[:120]}; exit_rows = [{"ticket": None, "kind": "check_failed", "status": "WARN", "note": str(e)[:80]}]
mt5.shutdown()

out_dir = Path("reports/signal_path"); out_dir.mkdir(parents=True, exist_ok=True)
tag = now.strftime("%Y-%m-%d")
alerts = [r for r in rows + exit_rows if r["status"] == "ALERT"]; warns = [r for r in rows + exit_rows if r["status"] == "WARN"]
summary = {"generated_at": now.isoformat(), "lookback_days": LOOKBACK_DAYS, "rows": rows,
           "exit_reconciliation": exit_summary, "exit_rows": exit_rows,
           "n_alert": len(alerts), "n_warn": len(warns)}
json.dump(summary, open(out_dir / f"{tag}.json", "w", encoding="utf-8"), indent=2)
json.dump(summary, open(out_dir / "latest.json", "w", encoding="utf-8"), indent=2)

print(f"SIGNAL PATH MONITOR  {tag}  (last {LOOKBACK_DAYS}d)   ALERT={len(alerts)}  WARN={len(warns)}")
print(f"{'instrument':9s} {'broker':10s} {'expected':>8s} {'observed':>8s}  status  note")
for r in rows:
    e = "-" if r["expected"] is None else r["expected"]; o = "-" if r["observed"] is None else r["observed"]
    print(f"{r['instrument']:9s} {str(r['broker']):10s} {str(e):>8s} {str(o):>8s}  {r['status']:6s}  {r['note']}")
print(f"\nEXIT RECONCILIATION (last {LOOKBACK_DAYS}d): {exit_summary}")
for r in exit_rows:
    print(f"  {r['status']:6s} {r['kind']:13s} ticket={r['ticket']}  {r['note']}")
if not exit_rows: print("  ok — every broker close has a journal exit")
sys.exit(2 if alerts else 0)
