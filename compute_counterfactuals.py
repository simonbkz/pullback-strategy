"""Fill in cf_outcome / cf_r_multiple for skipped signals.

For each skipped signal with an entry/sl/tp recorded, pull H1 bars from signal
time to +7 days and simulate: did price hit TP or SL first? Walk bar by bar
(conservative: assume adverse touches first within a bar).

Writes back to signals table so later queries can compute "what did we miss
by filtering?" statistics per skip_reason.

Fast enough to run on-demand (~1-5 min for ~600 pending signals).
"""
import sqlite3, json
from datetime import datetime, timezone, timedelta
import MetaTrader5 as mt5
import pandas as pd
import live_mt5_connector as cx

MAX_HOLD_BARS = 72  # ~3 days of H1 bars
FETCH_HOURS = int(MAX_HOLD_BARS * 2.6)  # wall-clock window wide enough to hold 72 TRADING bars across a weekend
# 2026-10-22 fixes: (1) resolver now shared with the trader (own copy picked HFM's
# trade-disabled plain symbol -> every FX counterfactual silently failed);
# (2) fetch window was 74 wall-clock hours, so weekend-adjacent and recent
# signals got <72 bars and were frozen as 'timeout' at mark-to-market (138 of
# 150 timeout rows were premature); now 'timeout' only when the full horizon
# has elapsed, otherwise the row stays NULL and is rescored on a later run.
DB = "trade_journal.db"
LOOKBACK_DAYS = 180  # was 45 — expanded 2026-10-18 after audit found 88 signals
                     # from May-August 2026 were excluded. Covers full signals history.

if not mt5.initialize():
    print("mt5 init fail", mt5.last_error()); raise SystemExit

cfg = json.load(open("mt5_runtime.json", encoding="utf-8"))
symmap = cfg.get("symbol_map", {})

def resolve_symbol(ins):
    return cx.resolve_symbol(ins, symmap)   # shared, trade-mode-aware resolver

def broker_offset(sym):
    # 2026-10-22: use the connector's validated server-wide offset. The old
    # per-symbol version read the last H1 bar of symbols not yet selected in
    # Market Watch (stale by hours/days) and produced cf_exit_ts in the future.
    return int(round(cx._get_broker_offset() / 3600))

db = sqlite3.connect(DB); db.row_factory = sqlite3.Row

# Pull all skipped signals needing CF computation that have enough data to simulate
rows = db.execute(
    "SELECT sig_id, ts, instrument, side, entry_price, sl, tp, planned_dist, skip_reason "
    "FROM signals WHERE decision='skipped' AND cf_outcome IS NULL "
    "AND entry_price IS NOT NULL AND sl IS NOT NULL AND tp IS NOT NULL "
    f"AND ts >= datetime('now','-{LOOKBACK_DAYS} days') "
    "ORDER BY ts"
).fetchall()
print(f"Processing {len(rows)} skipped signals (last {LOOKBACK_DAYS} days with price data)...")

# Cache broker offsets per symbol
_offset_cache = {}
def off(sym):
    if sym not in _offset_cache:
        _offset_cache[sym] = broker_offset(sym)
    return _offset_cache[sym]

n_done = 0; n_tp = 0; n_sl = 0; n_timeout = 0; n_fail = 0; n_pending = 0
for r in rows:
    ins = r["instrument"]; side = (r["side"] or "").lower()
    entry = float(r["entry_price"]); sl = float(r["sl"]); tp = float(r["tp"])
    sl_dist = abs(entry - sl)
    sym = resolve_symbol(ins)
    if sym is None or sl_dist <= 0:
        n_fail += 1; continue
    try:
        sig_dt = datetime.fromisoformat(r["ts"].replace("Z","+00:00"))
    except Exception:
        n_fail += 1; continue
    offset = off(sym)
    rates = mt5.copy_rates_range(sym, mt5.TIMEFRAME_H1,
        sig_dt - timedelta(hours=1 + offset),
        sig_dt + timedelta(hours=FETCH_HOURS + offset)
    )
    if rates is None or len(rates) == 0:
        n_fail += 1; continue
    df = pd.DataFrame(rates)
    # per-bar DST-aware conversion (2026-10-25): a scalar offset mislabels bars
    # on the other side of a DST flip by one hour
    df["time"] = pd.to_datetime(cx.broker_epochs_to_utc(df["time"].values), unit='s', utc=True)
    df = df.set_index("time").sort_index()
    seg = df[df.index > sig_dt].head(MAX_HOLD_BARS)
    if seg.empty:
        n_fail += 1; continue

    hit = None; r_mult = None; bars_to_exit = 0; exit_price = None; exit_ts = None
    for i, (ts, row) in enumerate(seg.iterrows()):
        hi = float(row["high"]); lo = float(row["low"])
        # First-touch rule: adverse before favorable within a bar (conservative)
        if side == "long":
            if lo <= sl:
                hit = "sl"; exit_price = sl; r_mult = -1.0; exit_ts = ts; bars_to_exit = i+1; break
            if hi >= tp:
                hit = "tp"; exit_price = tp; r_mult = abs(tp - entry)/sl_dist; exit_ts = ts; bars_to_exit = i+1; break
        else:
            if hi >= sl:
                hit = "sl"; exit_price = sl; r_mult = -1.0; exit_ts = ts; bars_to_exit = i+1; break
            if lo <= tp:
                hit = "tp"; exit_price = tp; r_mult = abs(entry - tp)/sl_dist; exit_ts = ts; bars_to_exit = i+1; break
    if hit is None and len(seg) < MAX_HOLD_BARS:
        # Horizon not elapsed yet (recent signal, or bars short) -- leave NULL,
        # rescore on a later run. Do NOT freeze a mark-to-market as 'timeout'.
        n_pending += 1
        continue
    if hit is None:
        # Still open at MAX_HOLD_BARS -- mark timeout, use close
        last = seg.iloc[-1]
        close_px = float(last["close"])
        if side == "long":
            r_mult = (close_px - entry)/sl_dist
        else:
            r_mult = (entry - close_px)/sl_dist
        hit = "timeout"; exit_price = close_px; exit_ts = seg.index[-1]; bars_to_exit = len(seg)
        n_timeout += 1
    elif hit == "tp": n_tp += 1
    elif hit == "sl": n_sl += 1

    db.execute(
        "UPDATE signals SET cf_outcome=?, cf_exit_price=?, cf_exit_ts=?, "
        "cf_r_multiple=?, cf_bars_to_exit=?, cf_computed_at=? "
        "WHERE sig_id=?",
        (hit, exit_price, exit_ts.isoformat() if exit_ts is not None else None,
         r_mult, bars_to_exit, datetime.now(timezone.utc).isoformat(), r["sig_id"])
    )
    n_done += 1
    if n_done % 50 == 0:
        db.commit()
        print(f"  processed {n_done}/{len(rows)}  (tp={n_tp}, sl={n_sl}, timeout={n_timeout}, fail={n_fail})")

db.commit()
print(f"\nDone: {n_done}/{len(rows)}  tp={n_tp}  sl={n_sl}  timeout={n_timeout}  pending(horizon not elapsed)={n_pending}  fail={n_fail}")
mt5.shutdown()

# ---- Analysis ----
print("\n" + "="*90)
print("COUNTERFACTUAL: what did each filter actually save/cost?")
print("="*90)
analysis = sqlite3.connect(DB); analysis.row_factory = sqlite3.Row
rows = analysis.execute(
    "SELECT skip_reason, COUNT(*) as n, "
    "SUM(CASE WHEN cf_outcome='tp' THEN 1 ELSE 0 END) as n_tp, "
    "SUM(CASE WHEN cf_outcome='sl' THEN 1 ELSE 0 END) as n_sl, "
    "SUM(CASE WHEN cf_outcome='timeout' THEN 1 ELSE 0 END) as n_to, "
    "AVG(cf_r_multiple) as avg_R, "
    "SUM(cf_r_multiple) as sum_R "
    "FROM signals WHERE decision='skipped' AND cf_outcome IS NOT NULL "
    f"AND ts >= datetime('now','-{LOOKBACK_DAYS} days') "
    "GROUP BY skip_reason ORDER BY sum_R DESC"
).fetchall()
print(f"\n{'skip_reason':40s} {'n':>5s} {'tp':>4s} {'sl':>4s} {'to':>4s} {'avg_R':>8s} {'sum_R':>8s}  interpretation")
for r in rows:
    name = (r["skip_reason"] or "")[:40]
    sum_R = r["sum_R"] or 0
    avg_R = r["avg_R"] or 0
    verdict = ""
    if r["n"] >= 10:
        if avg_R < -0.1: verdict = "FILTER WORKING (skips were losing)"
        elif avg_R > +0.1: verdict = "FILTER COSTING (skips would have won)"
        else: verdict = "neutral"
    print(f"{name:40s} {r['n']:>5d} {r['n_tp']:>4d} {r['n_sl']:>4d} {r['n_to']:>4d} "
          f"{avg_R:>+7.3f} {sum_R:>+7.2f}  {verdict}")
