"""Deterministic dossier for the daily agent audit. READ-ONLY. Prints a compact
fact sheet; the agent (agent_audit.ps1 -> claude -p) reads it and applies judgment.
Every section is a question that found a real bug in Oct 2026:
  A process/log health            D expected-vs-observed signals (forming bar STUBBED)
  B broker<->journal reconciliation (both sides)   E silent gates: cap / market-open / open-pos
  C today's signals, skips, trades F counterfactual table + drift   G throttles   H tasks
"""
import sys, json, sqlite3, re, statistics
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import MetaTrader5 as mt5, live_mt5_connector as cx, live_trader as lt
from config import DEFAULT_PARAMS

now = datetime.now(timezone.utc); cfg = json.load(open("mt5_runtime.json", encoding="utf-8"))
db = sqlite3.connect("trade_journal.db"); db.row_factory = sqlite3.Row
magic = int(cfg["execution"]["magic_number"]); day0 = now.strftime("%Y-%m-%dT00:00"); d24 = (now - timedelta(hours=24)).isoformat()
def H(t): print(f"\n=== {t} ===")
print(f"AGENT AUDIT DOSSIER  {now:%Y-%m-%d %H:%M} UTC ({now:%A})")

# ---- A: process + log health ----
H("A. PROCESS / LOG HEALTH")
import subprocess
ps = subprocess.run(["powershell", "-NoProfile", "-Command",
    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | % { ($_.CommandLine -replace '.*\\\\','') }"], capture_output=True, text=True).stdout.split()
print("python processes:", len(ps), sorted(set(ps)))
logs = sorted(Path("logs").glob("live_trader_2026*.log"), key=lambda p: p.stat().st_mtime)
# all logs touched in the last 24h (restarts split the day across files) -- audit finding 2026-10-22
recent = [p for p in logs if now.timestamp() - p.stat().st_mtime < 86400] or logs[-1:]
if logs:
    txt = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in recent); lines = txt.splitlines()
    err = [l for l in lines if re.search(r"Traceback|Error:|Exception:", l)]
    fails = [l for l in lines if "FAIL retcode" in l]
    caps = [l for l in lines if "concurrency cap" in l]
    print(f"logs(24h)={len(recent)} newest={logs[-1].name} lines={len(lines)} errors={len(err)} order_fails={len(fails)} cap_blocks={len(caps)} age_min={(now.timestamp()-logs[-1].stat().st_mtime)/60:.0f}")
    for l in (err[-3:] + fails[-3:] + caps[-2:]): print("   ", l.strip()[:160])
    skip_retry = {}
    for m in re.finditer(r"\[([A-Z0-9]+)\] skip-retry: (\w+)", txt): skip_retry[m.group(1)] = skip_retry.get(m.group(1), 0) + 1
    print("skip-retry by instrument:", dict(sorted(skip_retry.items(), key=lambda kv: -kv[1])[:6]))

# ---- B: reconciliation ----
H("B. BROKER <-> JOURNAL RECONCILIATION")
ok = mt5.initialize(); ai = mt5.account_info()
pos = [p for p in (mt5.positions_get() or []) if p.magic == magic]
j_open = {int(r[0]): dict(r) for r in db.execute("SELECT ticket, instrument FROM trades WHERE exit_ts IS NULL")}
b_open = {int(p.ticket) for p in pos}
deals = mt5.history_deals_get(now - timedelta(days=3), now + timedelta(hours=6)) or []
outs = {int(d.position_id): d for d in deals if d.magic == magic and d.entry == mt5.DEAL_ENTRY_OUT}
ins_ = {int(d.position_id): d for d in deals if d.magic == magic and d.entry == mt5.DEAL_ENTRY_IN}
j_known = {int(r[0]) for r in db.execute("SELECT ticket FROM trades")}
print(f"account: balance=${ai.balance:,.2f} equity=${ai.equity:,.2f} open_pnl=${ai.profit:+.2f}")
print(f"open: broker={len(b_open)} journal={len(j_open)}  phantom_open(journal open, broker closed)={sorted(set(j_open)-b_open)}  untracked(broker open, not in journal)={sorted(b_open-set(j_open))}")
print(f"last 3d: broker OUT deals={len(outs)} unjournaled_closes={sorted(set(outs)-j_known)}  broker IN deals={len(ins_)} unjournaled_entries={sorted(set(ins_)-j_known)}")
st = json.load(open("runtime_state.json", encoding="utf-8"))
print(f"runtime_state: position_meta={len(st.get('position_meta',{}))} be_armed={len(st.get('be_armed',[]))} equity_peak=${st.get('equity_peak',0):,.2f} DD_from_peak={(1-ai.equity/max(st.get('equity_peak',ai.equity),1))*100:.1f}%")

# ---- C: today's activity ----
H("C. LAST 24H ACTIVITY")
rows = db.execute("SELECT decision, skip_reason, COUNT(*) n FROM signals WHERE ts>=? GROUP BY 1,2 ORDER BY n DESC", (d24,)).fetchall()
print("signals:", [(r["decision"], r["skip_reason"], r["n"]) for r in rows])
tr = db.execute("SELECT instrument, side, substr(entry_ts,6,11) e, substr(exit_ts,6,11) x, outcome, ROUND(r_multiple_gross,2) R, ROUND(profit_usd,0) usd, effective_risk_pct risk, notes FROM trades WHERE entry_ts>=? OR exit_ts>=? ORDER BY entry_ts", (d24, d24)).fetchall()
for r in tr: print("   ", tuple(r))
cl = [r["R"] for r in tr if r["R"] is not None]
print(f"closed_24h: n={len(cl)} sumR={sum(cl):+.2f}" if cl else "closed_24h: none")

# ---- D: expected vs observed, forming bar stubbed ----
H("D. EXPECTED (strategy, as-seen-then) vs OBSERVED (journal) SIGNALS, last 24h")
so = cfg.get("strategy_overrides", {}); params = replace(DEFAULT_PARAMS, rr_target=float(so.get("rr_target", 2.5)), atr_stop_mult=float(so.get("atr_stop_mult", 2.0)))
enabled = [i for i in cfg["instruments"] if not (isinstance(cfg["tier_overrides"].get(i), dict) and cfg["tier_overrides"][i].get("enabled") is False)]
since = pd.Timestamp(now - timedelta(hours=24)).floor("h")
te = to = 0; missing = []; unresolved = []
for ins in enabled:
    b = cx.resolve_symbol(ins, cfg.get("symbol_map"))
    if not b: unresolved.append(ins); continue
    try:
        ltf = cx.fetch_bars(b, mt5.TIMEFRAME_H1, 1500); htf = cx.fetch_bars(b, mt5.TIMEFRAME_H4, 600)
        full = lt._get_entry_signal(cfg, ins, ltf, htf, params)
        cand = full.loc[(full.index >= since) & full["side"].isin(["long", "short"])].index
        fired = []
        for ts in cand:   # confirm each on bars as seen then (stub forming H1, rebuild forming H4)
            l1 = ltf[ltf.index <= ts + timedelta(hours=1)].copy()
            if len(l1) and l1.index[-1] == ts + timedelta(hours=1):
                o = l1.iloc[-1]["open"]
                for c in ("high", "low", "close"): l1.loc[l1.index[-1], c] = o
            h4 = ts.floor("4h"); hs = htf[htf.index < h4].copy(); win = ltf[(ltf.index >= h4) & (ltf.index <= ts)]
            if len(win): hs.loc[h4] = {"open": win.open.iloc[0], "high": win.high.max(), "low": win.low.min(), "close": win.close.iloc[-1], **{c: win[c].sum() for c in win.columns if c not in ("open","high","low","close")}}
            s = lt._get_entry_signal(cfg, ins, l1, hs, params)
            if ts in s.index and s.loc[ts, "side"] in ("long", "short"): fired.append(ts)
        obs = {r[0] for r in db.execute("SELECT ts FROM signals WHERE instrument=? AND ts>=?", (ins, since.isoformat()))}
        obs_ts = {pd.Timestamp(o) for o in obs}
        miss = [t for t in fired if t not in obs_ts]
        te += len(fired); to += len(obs)
        if fired or obs: print(f"   {ins:8s} expected={len(fired)} observed={len(obs)}  missing={[t.strftime('%d %H:%M') for t in miss]}")
        if miss: missing.append((ins, [t.strftime('%d %H:%M') for t in miss]))
    except Exception as e:
        print(f"   {ins:8s} ERR {str(e)[:80]}")
print(f"TOTAL expected={te} observed={to}  MISSING={missing}  unresolved={unresolved}")

# ---- E: silent gates ----
H("E. SILENT GATES (why a signal can vanish without a journal row)")
print(f"max_concurrent_positions={cfg['execution']['max_concurrent_positions']}  open_now={len(pos)}  (scale-in adds excluded in code)")
stale = []
for ins in enabled:
    b = cx.resolve_symbol(ins, cfg.get("symbol_map"))
    if b:
        okm, why = cx.is_market_open(b)
        if not okm: stale.append((ins, why))
print("is_market_open False now:", stale if stale else "none")
mt5.shutdown()

# ---- F: counterfactual table + pending ----
H("F. COUNTERFACTUALS (180d, scored only) + pending")
for r in db.execute("SELECT skip_reason, COUNT(*) n, ROUND(AVG(cf_r_multiple),3) a, ROUND(SUM(cf_r_multiple),2) s FROM signals WHERE decision='skipped' AND cf_outcome IS NOT NULL AND ts>=datetime('now','-180 days') GROUP BY 1 HAVING n>=5 ORDER BY s DESC"):
    print(f"   {r['skip_reason'][:38]:38s} n={r['n']:3d} avgR={r['a']:+.3f} sumR={r['s']:+.2f}")
pend = db.execute("SELECT COUNT(*) FROM signals WHERE decision='skipped' AND cf_outcome IS NULL AND entry_price IS NOT NULL AND ts>=datetime('now','-10 days')").fetchone()[0]
bad = db.execute("SELECT COUNT(*) FROM signals WHERE cf_outcome='timeout' AND cf_bars_to_exit<72").fetchone()[0]
fut = db.execute("SELECT COUNT(*) FROM signals WHERE cf_exit_ts > ?", (now.isoformat(),)).fetchone()[0]
print(f"pending(<10d)={pend}  premature_timeouts={bad} (must be 0)  future_exit_ts={fut} (must be 0)")
pm_miss = db.execute("SELECT COUNT(*) FROM trades WHERE exit_ts IS NOT NULL AND exit_ts>=datetime('now','-7 days') AND mae_R IS NULL").fetchone()[0]
pm_tot = db.execute("SELECT COUNT(*) FROM trades WHERE exit_ts IS NOT NULL AND exit_ts>=datetime('now','-7 days')").fetchone()[0]
print(f"post-mortem coverage last 7d: {pm_tot - pm_miss}/{pm_tot} closes have MAE/MFE + alt-exit replays (missing must be 0; the hook failed silently Oct 20-23)")

# ---- G: throttles + edge ----
H("G. THROTTLES / EDGE")
ps_, pr = lt._portfolio_regime_scale(cfg); print(f"portfolio_regime x{ps_} {pr}")
for ins in enabled:
    s, r = lt._inst_expectancy_scale(cfg, ins)
    if s != 1.0: print(f"   {ins:8s} x{s} {r}")
for ins in enabled:
    ex, why = lt._should_auto_exclude(cfg, ins)
    if ex: print(f"   {ins:8s} AUTO-EXCLUDED {why}")
for lbl, days in [("90d", 90), ("14d", 14)]:
    rs = [r[0] for r in db.execute("SELECT r_multiple_gross FROM trades WHERE r_multiple_gross IS NOT NULL AND entry_ts>=datetime('now',?)", (f"-{days} days",))]
    if len(rs) > 2: print(f"   {lbl}: n={len(rs)} meanR={statistics.mean(rs):+.3f} sd={statistics.stdev(rs):.2f} WR={sum(1 for x in rs if x>0)/len(rs):.0%}")

# ---- H: scheduled tasks ----
H("H. SCHEDULED TASKS (last result; 0=ok; signal-path 2=ALERT; fx-edge-monitor 1=yellow warnings only, 2=red; 267009=running now; 267011=never run / boot-triggered)")
out = subprocess.run(["powershell", "-NoProfile", "-Command",
    "Get-ScheduledTask | ? { $_.TaskName -like 'fx-edge*' } | Get-ScheduledTaskInfo | % { \"$($_.TaskName) $($_.LastRunTime.ToString('MM-dd HH:mm')) $($_.LastTaskResult)\" }"], capture_output=True, text=True).stdout
print(out.strip())
sp = Path("reports/signal_path/latest.json")
if sp.exists():
    j = json.load(open(sp, encoding="utf-8")); print(f"signal_path latest: {j.get('generated_at','')[:16]} alerts={j.get('n_alert')} warns={j.get('n_warn')} exit={j.get('exit_reconciliation')}")
print("\nEND OF DOSSIER")
