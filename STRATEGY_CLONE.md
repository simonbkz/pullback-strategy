# fx-edge — Strategy Clone & Transfer Guide

**Purpose:** Replicate the live-running fx-edge trading strategy on a new MT5 account (default example: XM Global). This document is written to be followable by a future Claude Code instance OR by a human.

**Source environment:** Windows Server 2022, Python 3.12, MetaTrader 5, HFMarkets broker account.
**Target example:** Any MT5-compatible broker — XM Global covered specifically.

---

## 1. What this strategy is (one-paragraph summary)

A multi-asset H1 trend-following system that:
- Uses H4 EMA50/EMA200 as trend filter, H1 pullback (default) / breakout / momentum / ensemble signals for entries
- Protects via a layered exit stack: BE-trail at +1.5R, chandelier at +2R, decay-SL, early_adverse_cut, position_loss_cap at 2.5R
- Scales risk per-instrument via live expectancy throttle (0× pause / 0.5× throttle / 1.0× / 1.25× boost / 1.5× boost)
- Auto-promotes shadow candidates to live via weekly walk-forward backtest with statistical gate (p ≤ 0.10)
- Runs fully autonomous with daily regime pulse, counterfactuals, news-blackout feed, and external health monitor

Full behavior history in `.claude/projects/.../memory/MEMORY.md`.

---

## 2. Prerequisites

**OS:** Windows (uses PowerShell + Task Scheduler + MT5 for Windows).
**Python:** 3.12 (recommended) or 3.11.
**MT5 terminal:** latest from MetaQuotes, installed from your broker's distribution for best IPC compatibility.
**Broker account:** MT5-compatible broker. Live account with funds.
**Paid APIs (optional but recommended):**
- EODHD (eodhd.com, ~$20/mo) — 5+ years of H1 history for backtests
- FRED (free) — US macro calendar
- Finnhub (free tier) — supplementary news

---

## 3. XM Global — broker-specific setup

### Account & terminal
1. Open an XM MT5 live account (Standard or Micro) at xm.com
2. Download the XM-branded MT5 terminal (ships with their server list configured)
3. Record: `login_number`, `password`, `server` (e.g., `XMGlobal-Real 3`)
4. Install MT5 to a dedicated folder (`C:\Program Files\MetaTrader 5 XM\` recommended) to avoid conflicts with other brokers
5. Enable **AutoTrading** (toolbar) and **Allow algorithmic trading** (Tools → Options → Expert Advisors → check "Allow algorithmic trading")

### Symbol naming on XM
XM Global uses **plain pair names** (no `r` suffix like HFM). Expect:
- FX: `EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`, `USDCAD`, `USDCHF`, `NZDUSD`
- Metals: `XAUUSD`, `XAGUSD`
- Oils: `USOIL`, `UKOIL` (or `WTI`, `BRENT` depending on account)
- Indices: `US500`, `US100`, `US30`, `DE40`, `UK100`, `JP225`, `AUS200`, `FRA40`
- Crypto: Check Market Watch for availability (XM Standard may not expose crypto directly)

**Verify symbols before deploy** — run `fetch_mt5_bars.py` or query via `symbols_get()` to confirm each name resolves.

### Account specifics (vs HFM source)
- **Leverage:** XM Standard 1000:1 (vs HFM 1000:1 — compatible)
- **Minimum lot:** 0.01 on both — compatible
- **Spread:** XM typically wider than HFM Pro accounts; may need to tune `max_spread_pips_overrides`

---

## 4. File inventory (what to copy)

### Core (REQUIRED)
```
live_trader.py                 # main loop + 10+ exit mechanisms + 9+ entry gates
live_mt5_connector.py          # broker interface (place/close/history)
strategy.py                    # pullback signal generator
strategy_breakout.py           # Donchian breakout
strategy_momentum.py           # 3-ATR 20-bar move
backtest.py                    # core backtesting engine
config.py                      # Instrument definitions, StrategyParams
trade_journal.py               # SQLite ops (signals + trades tables)
regime.py                      # regime detection
regime_predictor.py            # ML regime classifier (optional)
live_regime_manager.py         # regime refit scheduling
regime_novelty.py              # Mahalanobis distance filter (optional)
risk_scaling.py                # dynamic_risk() per-trade sizing
features.py                    # feature engineering for regime + ML
mt5_runtime.json               # master config (SEE SECTION 5)
```

### Autonomous infrastructure (strongly recommended)
```
regime_pulse.py                # daily health report
compute_counterfactuals.py     # grades filter efficacy
broader_walk_forward.py        # weekly broader WF with auto-promotion
mt5_history_fetch.py           # monthly MT5 cache refresh
health_pinger.py               # external monitor (needs healthchecks.io URL)
watchdog.py                    # process health + auto-restart
trade_monitor.py               # live monitor_status.json
news_feed.py                   # news blackout aggregator
news_sources/                  # directory: fred_calendar.py, central_banks.py, finnhub_calendar.py
config_lint.py                 # static config conflict checker
scripts/stat_audit.py          # statistical audit of strategy_per_instrument
```

### Optional
```
ml_models/                     # ML rescue models (keyed <INS>_<strategy>.joblib)
data_eodhd/                    # 5yr H1 history + _mapping.json
data_mt5/                      # MT5-cache H1/H4 history (regeneratable)
reports/                       # per-run reports from scheduled tasks
```

### DO NOT COPY
```
runtime_state.json             # per-account state — SHOULD regenerate
trade_journal.db               # per-account trade history — SHOULD regenerate
monitor_status.json            # per-account — regenerated
logs/                          # old logs irrelevant
backups/                       # old backups
```

---

## 5. `mt5_runtime.json` — key sections to translate

The source file is ~2500 lines; here are the sections that need **broker-specific translation**:

### 5.1 `connection` block
```json
"connection": {
  "path": "C:\\Program Files\\MetaTrader 5 XM\\terminal64.exe",
  "expected_server": "XMGlobal-Real 3",         <-- UPDATE
  "expected_login": YOUR_XM_LOGIN               <-- UPDATE
}
```

### 5.2 `symbol_map` block
Translate each HFM `*r` suffix to XM plain name. Example:
```json
"symbol_map": {
  "EURUSD": ["EURUSD"],              // XM: no suffix
  "GBPUSD": ["GBPUSD"],
  "XAUUSD": ["XAUUSD", "GOLD"],      // some brokers call it GOLD
  "ETHUSD": ["ETHUSD"],              // XM may not have crypto — check Market Watch
  "US500": ["US500Cash", "SPX500"]   // XM may name differently
}
```
**Verify each with `mt5.symbol_info(name)` before go-live.**

### 5.3 `tier.base_risk_pct`
Default 6%. Review this for your target risk tolerance. Lower = more conservative.

### 5.4 `instruments` list
Keep this as-is OR trim to the subset XM supports. Any instrument not available on XM should either be removed or marked `tier_overrides.<INS>.enabled = false`.

### 5.5 `execution.max_spread_pips_overrides`
XM Standard spreads are wider than HFM Pro. Expect ~1.5-2× multiplier on each entry. Start loose, tune down based on actual observed spreads.

### 5.6 `external_monitor.webhook_url`
Create your own healthchecks.io URL (free, 2 min, see memory `reference_external_monitor.md`).

---

## 6. Deployment sequence (step-by-step)

Run as Administrator in PowerShell.

### 6.1 Install dependencies
```powershell
cd C:\Users\YourUser\fx-edge-xm   # target folder
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install MetaTrader5 pandas numpy scikit-learn joblib scipy pyarrow requests
```

### 6.2 Verify MT5 can connect
```python
# In .venv Python shell:
import MetaTrader5 as mt5
mt5.initialize()
print(mt5.account_info())    # should show your XM account
print(mt5.symbol_info("EURUSD"))   # should return a symbol
mt5.shutdown()
```

### 6.3 Translate config
Edit `mt5_runtime.json`:
- `connection.path`, `expected_server`, `expected_login`
- `symbol_map` — verify every entry resolves via `mt5.symbol_info()`
- `execution.max_spread_pips_overrides` — set wider initially (e.g., 2× HFM values)
- Any `tier_overrides.<INS>.enabled = false` for symbols XM doesn't support
- `external_monitor.webhook_url` + `enabled: true`

### 6.4 Validate config
```powershell
python config_lint.py
```
Must show `0 errors`. Fix any ERROR findings before continuing.

### 6.5 Dry-run signal evaluation
Launch trader in dry-run mode (edit cfg `"dry_run": true`, restart) and watch logs. Confirm:
- All instruments resolve to broker symbols on startup
- Heartbeats appear every 15 min
- Signals get generated and logged (even if dry_run skips the actual order placement)

### 6.6 Set `"dry_run": false` and go live
After 1-2 bars of dry-run look clean, flip `dry_run: false` in config. Hot-reload picks it up.

### 6.7 Register scheduled tasks
See section 7.

### 6.8 Set up external monitor
Sign up healthchecks.io, create check, paste URL into config, flip `external_monitor.enabled: true`.

---

## 7. Scheduled tasks — PowerShell registration

All with `LogonType=S4U` (no password needed, runs without logon).

```powershell
$workdir = (Get-Location).Path
$py = (Resolve-Path .\.venv\Scripts\python.exe).Path

# Common pattern:
function Register-FxEdgeTask($name, $script, $trigger, $argsExtra = "") {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    $action = New-ScheduledTaskAction -Execute $py -Argument "$script $argsExtra" -WorkingDirectory $workdir
    $principal = New-ScheduledTaskPrincipal -UserId "Administrator" -LogonType S4U -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 365) `
        -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings
}

# Boot-triggered (auto-recovery):
$mt5Trigger = New-ScheduledTaskTrigger -AtStartup; $mt5Trigger.Delay = "PT30S"
$ltTrigger = New-ScheduledTaskTrigger -AtStartup; $ltTrigger.Delay = "PT3M"
$wdTrigger = New-ScheduledTaskTrigger -AtStartup; $wdTrigger.Delay = "PT4M"
$mnTrigger = New-ScheduledTaskTrigger -AtStartup; $mnTrigger.Delay = "PT4M30S"
Register-FxEdgeTask "fx-edge-mt5" "C:\Program Files\MetaTrader 5 XM\terminal64.exe" $mt5Trigger
Register-FxEdgeTask "fx-edge-live-trader" (Resolve-Path .\live_trader.py).Path $ltTrigger
Register-FxEdgeTask "fx-edge-watchdog" (Resolve-Path .\watchdog.py).Path $wdTrigger
Register-FxEdgeTask "fx-edge-trade-monitor" (Resolve-Path .\trade_monitor.py).Path $mnTrigger

# Daily feedback loops:
Register-FxEdgeTask "fx-edge-regime-pulse" (Resolve-Path .\regime_pulse.py).Path `
    (New-ScheduledTaskTrigger -Daily -At "06:00")
Register-FxEdgeTask "fx-edge-counterfactuals" (Resolve-Path .\compute_counterfactuals.py).Path `
    (New-ScheduledTaskTrigger -Daily -At "06:15")
Register-FxEdgeTask "fx-edge-news-feed" (Resolve-Path .\news_feed.py).Path `
    (New-ScheduledTaskTrigger -Daily -At "06:00")
Register-FxEdgeTask "fx-edge-health-pinger" (Resolve-Path .\health_pinger.py).Path `
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
     -RepetitionInterval (New-TimeSpan -Minutes 5) `
     -RepetitionDuration (New-TimeSpan -Days 3650))

# Weekly / monthly:
Register-FxEdgeTask "fx-edge-broader-wf" (Resolve-Path .\broader_walk_forward.py).Path `
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "05:30")
Register-FxEdgeTask "fx-edge-mt5-history-fetch" (Resolve-Path .\mt5_history_fetch.py).Path `
    (New-ScheduledTaskTrigger -Weekly -WeeksInterval 4 -DaysOfWeek Sunday -At "04:30")
```

---

## 8. First-run validation checklist

After section 6+7, verify in this order:

1. **Processes alive after launch:**
   ```powershell
   (Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Measure-Object).Count   # expect >= 6
   (Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" | Measure-Object).Count  # expect 1
   ```
2. **Trader log fresh (within 60s):**
   ```powershell
   $log = Get-ChildItem logs\live_trader_*.log | Sort-Object LastWriteTime -Desc | Select-Object -First 1
   "age=$([int]((Get-Date) - $log.LastWriteTime).TotalSeconds)s"
   ```
3. **Zero errors in startup log:**
   ```powershell
   (Select-String -Path $log.FullName -Pattern "Traceback|Error:|Exception:" -CaseSensitive | Measure-Object).Count
   ```
4. **All instruments resolved to broker symbols:**
   Look for `[ok] XAUUSD -> ...` lines in trader startup output. Any `[fail]` or `[skip]` lines indicate broker-symbol-map mismatch — fix `symbol_map` in config.
5. **monitor_status.json refreshes within 60s:**
   ```powershell
   [int]((Get-Date) - (Get-Item monitor_status.json).LastWriteTime).TotalSeconds   # < 60
   ```
6. **First heartbeat printed within 15 min:**
   ```powershell
   Select-String -Path $log.FullName -Pattern "heartbeat" | Select-Object -Last 3
   ```
7. **Config lint passes:**
   ```powershell
   python config_lint.py   # must exit code 0
   ```
8. **External monitor sends first ping:**
   ```powershell
   python health_pinger.py   # should show "HTTP 200 OK"
   ```
   Check healthchecks.io dashboard shows green ping received.

---

## 9. Operation playbook — what to watch

**Daily:** `regime_pulse.json` headline flags. If any `losing_regime` + `dormant` instrument appears, review for config change.

**Weekly:** `reports/broader_wf/latest.json`. Check for new deploy-ready candidates. Auto-promotion fires after 2 consecutive clearances.

**On outlier losses:** `regime_pulse.json` flags any realized loss > 1.5R. Investigate — typically crypto gap-slip or news event.

**On alert from healthchecks.io:** server unreachable OR trader died. Login via remote desktop, check processes, check logs. Scheduled task should auto-restart within 5 min.

---

## 10. Known risks and mitigations (as of 2026-10-20)

| Risk | Mechanism | Mitigation (already in config) |
|---|---|---|
| Crypto gap-slip (price jumps through SL) | Thin 24/7 liquidity | position_loss_cap at 2.5R; crypto at nano-shadow 0.05× risk |
| Broker spread spikes blocking signals | Overnight / low-liquidity windows | hour_filter blocks 23-05 UTC; UK100 atr_stop_mult widened |
| Backtest-vs-live divergence | Regime shifts; sim slippage ignored | regime_pulse flags drift; auto_exclusion disables losing+dormant |
| Hidden config conflicts | Interacting filters (e.g., MSC-vs-ensemble) | config_lint.py pre-deploy check |
| Server outage during trade | Local management pauses | boot-triggered S4U scheduled tasks auto-restore; broker-side SL still fires; external monitor alerts |
| Hidden strategy drift | Fixed thresholds becoming stale | weekly broader_wf re-tests + stat gate; auto-promotion/demotion ladder |

---

## 11. Troubleshooting

**`[FAIL] <INS>: symbol not available`** in trader log → `symbol_map` needs correction; use `mt5.symbol_info()` interactively to find correct broker name.

**`retcode=10016 (Invalid stops)`** on order placement → `atr_stop_mult` too tight for this broker's stops_level. Add per-instrument override in `tier_overrides.<INS>.atr_stop_mult = 4.0` (or higher).

**No signals firing on an enabled instrument for days** → Check: (a) `inst_expectancy_pause` fired (seen in log), (b) `auto_exclusion` fired, (c) multi_signal_confirmation accidentally enabled. Run `config_lint.py`.

**Scheduled task shows `LastResult=267011`** → "Task has not yet run" — means boot never happened (expected for boot-triggered tasks) OR LogonType is Interactive (fix: re-register with S4U principal).

**Trader PID counts != 6** → venv launcher creates stub-child pairs. 3 processes × 2 PIDs = 6. If < 6, one process died — check logs for crash.

**Signals table has entries but counterfactual coverage is 0** → check `compute_counterfactuals.py` lookback window (`LOOKBACK_DAYS`). Default 180 should cover most cases.

---

## 12. Memory / historical context (if cloning via Claude)

The `.claude/projects/.../memory/MEMORY.md` directory contains ~40 memory files documenting every strategy decision from April through October 2026. Transfer these ONLY if you're cloning on the same user's machine (via the `~/.claude/` structure); otherwise they're not directly portable but can serve as reference reading.

Key memory files to read first for context:
- `fx_edge_project.md` — overall project arc
- `project_grand_sweep_winner.md` — current config winner (base_risk tuning)
- `project_counterfactuals_dow_oct08.md` — filter efficacy audit framework
- `project_universe_expansion_oct13.md` + `project_universe_fill_oct18.md` — current universe state
- `reference_external_monitor.md` — health monitor setup
- `reference_regime_pulse.md` — daily report schema

---

## 13. Rollback / abort

If deployment goes wrong at any step:

1. **Stop everything:**
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Stop-Process -Force
   ```
2. **Disable all scheduled tasks:**
   ```powershell
   Get-ScheduledTask | Where-Object { $_.TaskName -like "fx-edge*" } | Disable-ScheduledTask
   ```
3. **Close positions manually in MT5 if needed** (do NOT withdraw/transfer until positions are flat)

System is now cold. Diagnose, fix, re-enable.

---

## 14. Final sanity notes

- **This is live-money software.** Test with small `base_risk_pct` (e.g., 1-2%) for the first 50 trades to validate on the new broker before scaling up.
- **Always keep broker-side SL/TP active.** The strategy relies on them as the last line of defense during local outages.
- **Memory + journal are the source of truth** for what the strategy has done. The config is only the current intent.
- **New broker = new regime.** Expect first 30-50 trades to be noisy; don't tune based on < 20-trade samples.

---

**Last updated:** 2026-10-20
**Source environment:** fx-edge/ on HFMarketsSA-Live2 account
**Document version:** 1.0
