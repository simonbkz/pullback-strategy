"""Backfill per-trade post-mortems (MAE/MFE/alt-exit replays) for closed trades that
have none -- the live hook silently failed on NameError from 2026-10-20 to 2026-10-23.
Uses broker deal times (both broker-time, consistent with _postmortem_for_ticket). Idempotent."""
import sys, json, sqlite3
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import MetaTrader5 as mt5, live_trader as lt, trade_journal as journal
cfg = json.load(open("mt5_runtime.json", encoding="utf-8"))
if not mt5.initialize(): print("mt5 init fail"); sys.exit(1)
db = sqlite3.connect("trade_journal.db"); db.row_factory = sqlite3.Row
rows = db.execute("SELECT ticket, instrument, broker_symbol, side, entry_price, planned_dist FROM trades WHERE exit_ts IS NOT NULL AND mae_R IS NULL AND exit_ts>='2026-07-05' ORDER BY exit_ts").fetchall()
print(f"{len(rows)} closed trades without post-mortem"); n = 0
for r in rows:
    t = int(r["ticket"]); deals = mt5.history_deals_get(position=t) or []
    inn = next((d for d in deals if d.entry == mt5.DEAL_ENTRY_IN), None); out = next((d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT), None)
    if not inn or not out: print(f"  {t} {r['instrument']}: deals missing"); continue
    meta = {"side": r["side"], "entry_price": r["entry_price"], "planned_dist": r["planned_dist"], "broker": r["broker_symbol"], "ins": r["instrument"]}
    pm = lt._postmortem_for_ticket(t, meta, float(inn.time), float(out.time), cfg)
    if pm: journal.update_postmortem(t, **pm); n += 1; print(f"  {t} {r['instrument']:7s} MAE={pm.get('mae_R',0):.2f} MFE={pm.get('mfe_R',0):.2f} be1.0={pm.get('alt_be10_R',0):+.2f} noch={pm.get('alt_noch_R',0):+.2f}")
    else: print(f"  {t} {r['instrument']}: no result (bars short?)")
mt5.shutdown(); print(f"backfilled {n}")
