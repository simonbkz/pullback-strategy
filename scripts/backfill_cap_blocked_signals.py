"""Backfill signal rows for entries the concurrency cap silently dropped (one-off,
2026-10-22). Regenerates each signal on bars as seen at the time (forming bar
stubbed), logs it as decision='skipped' reason='concurrency_cap' so
compute_counterfactuals.py scores it like any other skip. Idempotent: skips
(ts, instrument) pairs already present."""
import sys, json, sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import MetaTrader5 as mt5, live_mt5_connector as cx, live_trader as lt, trade_journal as journal
from config import DEFAULT_PARAMS

BLOCKED = [("ETHUSD", "2026-10-22 08:00"), ("BTCUSD", "2026-10-22 09:00"), ("EURUSD", "2026-10-22 08:00"),
           ("NZDJPY", "2026-10-22 08:00"), ("CHFJPY", "2026-10-22 12:00"), ("USOIL", "2026-10-22 11:00"), ("GBPJPY", "2026-10-22 11:00"),
           # Oct 21 evening, same cap (JP225+UK100+BTCUSD open 15:01 -> 03:00 UTC) -- found by the first agent audit
           ("US100", "2026-10-21 17:00"), ("XAUUSD", "2026-10-21 18:00"), ("XAUUSD", "2026-10-21 22:00"), ("EURUSD", "2026-10-21 18:00"),
           ("US30", "2026-10-21 19:00"), ("EURJPY", "2026-10-21 20:00"), ("GBPJPY", "2026-10-21 21:00"), ("JP225", "2026-10-22 00:00"), ("UK100", "2026-10-22 00:00")]
cfg = json.load(open("mt5_runtime.json", encoding="utf-8")); so = cfg.get("strategy_overrides", {})
params = replace(DEFAULT_PARAMS, rr_target=float(so.get("rr_target", 2.5)), atr_stop_mult=float(so.get("atr_stop_mult", 2.0)))
db = sqlite3.connect("trade_journal.db")
if not mt5.initialize(): print("mt5 init fail"); sys.exit(1)
n = 0
for ins, when in BLOCKED:
    ts = pd.Timestamp(when, tz="UTC")
    if db.execute("SELECT 1 FROM signals WHERE instrument=? AND ts=?", (ins, ts.isoformat())).fetchone():
        print(f"  {ins} {when} already present -- skip"); continue
    b = cx.resolve_symbol(ins, cfg.get("symbol_map"))
    ltf = cx.fetch_bars(b, mt5.TIMEFRAME_H1, 1500); htf = cx.fetch_bars(b, mt5.TIMEFRAME_H4, 600)
    l1 = ltf[ltf.index <= ts + timedelta(hours=1)].copy()
    if l1.index[-1] == ts + timedelta(hours=1):
        o = l1.iloc[-1]["open"]
        for c in ("high", "low", "close"): l1.loc[l1.index[-1], c] = o
    h4 = ts.floor("4h"); hs = htf[htf.index < h4].copy(); win = ltf[(ltf.index >= h4) & (ltf.index <= ts)]
    if len(win): hs.loc[h4] = {"open": win.open.iloc[0], "high": win.high.max(), "low": win.low.min(), "close": win.close.iloc[-1], **{c: win[c].sum() for c in win.columns if c not in ("open","high","low","close")}}
    sig = lt._get_entry_signal(cfg, ins, l1, hs, params); row = sig.loc[ts]
    if pd.isna(row["side"]): print(f"  {ins} {when}: no signal on as-seen bars -- skip"); continue
    side, sl, tp = row["side"], float(row["sl"]), float(row["tp"])
    nxt = ltf[ltf.index > ts]; info = mt5.symbol_info(b)
    entry = float(nxt.open.iloc[0]) + (info.spread * info.point / 2) * (1 if side == "long" else -1)
    journal.log_signal(ts=ts, instrument=ins, side=side, entry_price=entry, sl=sl, tp=tp,
                       planned_dist=abs(sl - tp) / 2.5, decision="skipped", skip_reason="concurrency_cap", ticket=None)
    print(f"  {ins} {when} {side:5s} entry={entry:.5f} sl={sl:.5f} tp={tp:.5f}  -> logged concurrency_cap"); n += 1
mt5.shutdown(); db.close()
print(f"backfilled {n} rows")
