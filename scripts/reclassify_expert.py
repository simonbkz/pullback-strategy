"""Re-label journal rows whose outcome is the catch-all 'expert' using the fixed
_classify_outcome (comment-prefix checks now precede the EXPERT reason check).
Also flips consecutive-loss bookkeeping if a row changes between win/loss sets —
it doesn't; this only relabels. Idempotent."""
import sys, sqlite3
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import MetaTrader5 as mt5
import live_trader as lt

if not mt5.initialize(): print("mt5 init fail"); sys.exit(1)
db = sqlite3.connect("trade_journal.db"); db.row_factory = sqlite3.Row
rows = db.execute("SELECT ticket, instrument, side, planned_dist FROM trades WHERE outcome='expert'").fetchall()
print(f"{len(rows)} rows labelled 'expert'")
changed = {}
for r in rows:
    t = int(r["ticket"])
    out = next((d for d in (mt5.history_deals_get(position=t) or []) if d.entry == mt5.DEAL_ENTRY_OUT), None)
    if out is None: print(f"  {t} no OUT deal"); continue
    new = lt._classify_outcome(out, r["planned_dist"], None, r["side"], out.price)
    comment = (getattr(out, "comment", "") or "").strip()
    print(f"  {t} {r['instrument']:7s} comment='{comment}' -> {new}")
    if new != "expert":
        db.execute("UPDATE trades SET outcome=? WHERE ticket=?", (new, t)); changed[new] = changed.get(new, 0) + 1
db.commit(); mt5.shutdown()
print(f"relabelled: {changed if changed else 'none (comments carry no handler prefix)'}")
