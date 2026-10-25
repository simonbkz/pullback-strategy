"""Reconcile journal rows that have no exit_ts against MT5 deal history.

For each trade with exit_ts IS NULL: if MT5 has a DEAL_ENTRY_OUT deal for that
position ticket, log the close (same classifier as live) and run the post-mortem.
If the position is genuinely still open on the broker, leave it. If MT5 has no
record at all, report it.

Safe to re-run. Idempotent. Run after a journal backup.
"""
import sys, json, sqlite3
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import MetaTrader5 as mt5
import live_mt5_connector as cx
import live_trader as lt
import trade_journal as journal

if not mt5.initialize(): print("mt5 init fail"); sys.exit(1)
cfg = json.load(open("mt5_runtime.json", encoding="utf-8"))
db = sqlite3.connect("trade_journal.db"); db.row_factory = sqlite3.Row
rows = db.execute("SELECT ticket, instrument, broker_symbol, side, entry_price, sl_planned, sl_initial, planned_dist "
                  "FROM trades WHERE exit_ts IS NULL ORDER BY entry_ts").fetchall()
open_tickets = {p.ticket for p in (mt5.positions_get() or [])}
print(f"{len(rows)} journal rows without exit; {len(open_tickets)} positions actually open on broker\n")
n_fixed = n_open = n_missing = 0
for r in rows:
    t = int(r["ticket"])
    if t in open_tickets:
        print(f"  {t} {r['instrument']:7s} still OPEN on broker — leaving"); n_open += 1; continue
    deals = mt5.history_deals_get(position=t) or []
    out = next((d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT), None)
    inn = next((d for d in deals if d.entry == mt5.DEAL_ENTRY_IN), None)
    if out is None:
        print(f"  {t} {r['instrument']:7s} NO close deal in MT5 history ({len(deals)} deals) — cannot reconcile"); n_missing += 1; continue
    outcome = lt._classify_outcome(out, r["planned_dist"], None, r["side"], out.price)
    journal.log_trade_close(ticket=t, exit_ts=float(out.time) - cx._get_broker_offset(), exit_price=float(out.price), outcome=outcome,
                            profit_usd=float(out.profit), commission_usd=float(getattr(out,"commission",0.0) or 0.0),
                            swap_usd=float(getattr(out,"swap",0.0) or 0.0), sl_at_exit=None,
                            be_armed=False, adaptive_tighten=False, delay_swap_fired=False)
    meta = {"side": r["side"], "entry_price": r["entry_price"], "planned_dist": r["planned_dist"],
            "broker": r["broker_symbol"], "ins": r["instrument"]}
    pm = lt._postmortem_for_ticket(t, meta, float(inn.time) if inn else float(out.time) - 3600, float(out.time), cfg) if inn else {}
    if pm: journal.update_postmortem(t, **pm)
    R = db.execute("SELECT r_multiple_gross FROM trades WHERE ticket=?", (t,)).fetchone()[0]
    print(f"  {t} {r['instrument']:7s} CLOSED {outcome:8s} ${out.profit:+.2f}  R={R if R is not None else '?'}  "
          f"{'pm:MAE=%.2f MFE=%.2f' % (pm.get('mae_R',0), pm.get('mfe_R',0)) if pm else 'no postmortem'}")
    n_fixed += 1
mt5.shutdown()
print(f"\nreconciled={n_fixed}  still_open={n_open}  unresolvable={n_missing}")
