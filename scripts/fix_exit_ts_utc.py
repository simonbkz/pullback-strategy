"""One-off 2026-10-22: trades.exit_ts was written from deal.time (BROKER time,
GMT+2/+3) while entry_ts / signals.ts are UTC. Re-derive exit_ts for every closed
trade from the broker OUT deal, converting with the offset that applied on that
date (HFM server = EET: GMT+3 from last Sunday of March to last Sunday of October,
else GMT+2). Re-derivation, not shifting, so it is idempotent. Backs up the DB.
Rows whose OUT deal cannot be found are left unchanged and listed."""
import sys, sqlite3, shutil, calendar
from datetime import datetime, timezone, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import MetaTrader5 as mt5
root = Path(__file__).resolve().parent.parent; db_path = root / "trade_journal.db"
def last_sunday(y, m):
    d = max(wk[-1] for wk in calendar.monthcalendar(y, m)); return datetime(y, m, d, tzinfo=timezone.utc)
def eet_offset_hours(dt_broker_label):
    y = dt_broker_label.year
    start = last_sunday(y, 3) + timedelta(hours=3); end = last_sunday(y, 10) + timedelta(hours=4)
    return 3 if start <= dt_broker_label < end else 2
if not mt5.initialize(): print("mt5 init fail"); sys.exit(1)
bak = root / f"trade_journal.db.bak_exitts_{datetime.now():%Y%m%d_%H%M%S}"; shutil.copy2(db_path, bak); print("backup:", bak.name)
db = sqlite3.connect(db_path); db.row_factory = sqlite3.Row
rows = db.execute("SELECT ticket, exit_ts FROM trades WHERE exit_ts IS NOT NULL").fetchall()
n_fix = n_same = n_miss = 0; missing = []
for r in rows:
    t = int(r["ticket"]); out = next((d for d in (mt5.history_deals_get(position=t) or []) if d.entry == mt5.DEAL_ENTRY_OUT), None)
    if out is None: n_miss += 1; missing.append(t); continue
    label = datetime.fromtimestamp(int(out.time), tz=timezone.utc)      # broker label as if UTC
    utc = label - timedelta(hours=eet_offset_hours(label))
    new = utc.isoformat()
    if new == r["exit_ts"]: n_same += 1; continue
    db.execute("UPDATE trades SET exit_ts=? WHERE ticket=?", (new, t)); n_fix += 1
db.commit(); mt5.shutdown()
print(f"rows={len(rows)} rewritten={n_fix} already_utc={n_same} no_deal={n_miss} {missing[:10]}")
chk = db.execute("SELECT COUNT(*) FROM trades WHERE exit_ts IS NOT NULL AND exit_ts < entry_ts").fetchone()[0]
print(f"sanity: exit_ts < entry_ts rows = {chk} (expect 0)")
