"""One-off 2026-10-22 (companion to fix_exit_ts_utc.py): rows journaled by the
auto-adopt path had entry_ts = position.time (BROKER time). Re-derive entry_ts
as UTC from the broker IN deal, but ONLY where the stored entry_ts equals the
broker label (within 120 s) -- rows written from datetime.now() are already UTC
and are left alone. Idempotent. Backs up the DB."""
import sys, sqlite3, shutil, calendar
from datetime import datetime, timezone, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import MetaTrader5 as mt5
root = Path(__file__).resolve().parent.parent; db_path = root / "trade_journal.db"
def last_sunday(y, m):
    d = max(wk[-1] for wk in calendar.monthcalendar(y, m)); return datetime(y, m, d, tzinfo=timezone.utc)
def eet_offset_hours(label):
    y = label.year; start = last_sunday(y, 3) + timedelta(hours=3); end = last_sunday(y, 10) + timedelta(hours=4)
    return 3 if start <= label < end else 2
if not mt5.initialize(): print("mt5 init fail"); sys.exit(1)
bak = root / f"trade_journal.db.bak_entryts_{datetime.now():%Y%m%d_%H%M%S}"; shutil.copy2(db_path, bak); print("backup:", bak.name)
db = sqlite3.connect(db_path); db.row_factory = sqlite3.Row
rows = db.execute("SELECT ticket, entry_ts, notes FROM trades").fetchall()
n_fix = n_utc = n_miss = 0; fixed_notes = {}
for r in rows:
    t = int(r["ticket"]); inn = next((d for d in (mt5.history_deals_get(position=t) or []) if d.entry == mt5.DEAL_ENTRY_IN), None)
    if inn is None: n_miss += 1; continue
    label = datetime.fromtimestamp(int(inn.time), tz=timezone.utc)
    try: stored = datetime.fromisoformat(r["entry_ts"].replace("Z", "+00:00"))
    except Exception: n_miss += 1; continue
    if stored.tzinfo is None: stored = stored.replace(tzinfo=timezone.utc)
    if abs((stored - label).total_seconds()) <= 120:          # stored == broker label -> was written from position.time
        utc = label - timedelta(hours=eet_offset_hours(label))
        db.execute("UPDATE trades SET entry_ts=? WHERE ticket=?", (utc.isoformat(), t)); n_fix += 1
        k = (r["notes"] or "")[:14]; fixed_notes[k] = fixed_notes.get(k, 0) + 1
    else:
        n_utc += 1
db.commit(); mt5.shutdown()
print(f"rows={len(rows)} rewritten={n_fix} by_notes={fixed_notes} already_utc={n_utc} no_deal={n_miss}")
chk = db.execute("SELECT COUNT(*) FROM trades WHERE exit_ts IS NOT NULL AND exit_ts < entry_ts").fetchone()[0]
print(f"sanity: exit_ts < entry_ts rows = {chk} (expect 0)")
for r in db.execute("SELECT ticket, instrument, entry_ts, exit_ts, notes FROM trades WHERE exit_ts IS NOT NULL AND exit_ts < entry_ts LIMIT 5"):
    print("   still inverted:", tuple(r))
