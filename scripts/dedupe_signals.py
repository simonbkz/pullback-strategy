"""One-off 2026-10-22: remove duplicate skipped-signal rows (same bar, instrument,
side, skip_reason) that non-bar-burning gates re-logged every poll. Keeps the
earliest row (lowest sig_id). Backs up the DB first. Idempotent."""
import sqlite3, shutil
from datetime import datetime
from pathlib import Path
root = Path(__file__).resolve().parent.parent; db_path = root / "trade_journal.db"
bak = root / f"trade_journal.db.bak_dedupe_{datetime.now():%Y%m%d_%H%M%S}"; shutil.copy2(db_path, bak); print("backup:", bak.name)
db = sqlite3.connect(db_path)
before = db.execute("SELECT COUNT(*) FROM signals WHERE decision='skipped'").fetchone()[0]
groups = db.execute("SELECT skip_reason, COUNT(*)-COUNT(DISTINCT ts||'|'||instrument||'|'||IFNULL(side,'')) extra FROM signals WHERE decision='skipped' GROUP BY 1 HAVING extra>0 ORDER BY extra DESC").fetchall()
for g in groups: print(f"  {g[0][:40]:40s} duplicates={g[1]}")
n = db.execute("""DELETE FROM signals WHERE decision='skipped' AND sig_id NOT IN (
    SELECT MIN(sig_id) FROM signals WHERE decision='skipped' GROUP BY ts, instrument, IFNULL(side,''), IFNULL(skip_reason,''))""").rowcount
db.commit()
after = db.execute("SELECT COUNT(*) FROM signals WHERE decision='skipped'").fetchone()[0]
print(f"skipped rows: {before} -> {after}  (deleted {n})")
