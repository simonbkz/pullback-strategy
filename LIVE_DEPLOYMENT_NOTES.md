# Live deployment notes — V_BASE winner rollout

**Backtest target**: V_NOV_dn @ stale=72, base risk=5.70%, novelty floor=0.5
on the 9-instrument basket (HK50 unavailable on HFM, so live runs 8). Test
period (last 30% of cached H1 data, ~late 2025–2026) returned **+65,907% / 59.92% DD**.

**This is aggressive.** 5.70% per-trade risk is **2.85× the current live config**
(2.0%). A single -3R outcome on a tail event = ~17% account loss in one trade.
Do NOT skip the phased rollout below.

## What was added to live trading code

| Change | File | Default |
|---|---|---|
| `novelty_multiplier` kwarg in `dynamic_risk()` | [risk_scaling.py](risk_scaling.py) | 1.0 (no change) |
| `save_baselines_to_disk` / `load_baselines_from_disk` / `novelty_multiplier_for_features` / `fit_per_instrument_baselines` | [regime_novelty.py](regime_novelty.py) | new functions |
| `state.novelty_baselines`, `load_novelty_baselines()` | [live_trader.py](live_trader.py) | None when disabled |
| Novelty multiplier applied at signal time before `dynamic_risk()` | [live_trader.py](live_trader.py) | gated by `novelty.enabled` |
| `process_stale_exit()` polled in main loop | [live_trader.py](live_trader.py) | gated by `stale_exit.enabled` |
| `save_novelty_baselines.py` runner | new | n/a |
| `mt5_runtime_aggressive.json` config | new | flags off; risk 5.70% |

**Existing behavior is unchanged** when running with the original
`mt5_runtime.json` — the new `novelty` and `stale_exit` config sections are
absent there, so both features are inert.

## Phased rollout

Each phase: smallest broker lot size, 1 week of live observation, journal
review before next phase. Roll back by reverting `--config` to the prior file.

### Phase 0 — pre-flight (today)

1. **Smoke test in dry-run** with current conservative config to verify no
   regression from the additive code changes:
   ```powershell
   .\.venv\Scripts\python.exe -u live_trader.py --dry-run
   ```
   Expected: identical behavior to before. Code paths that compute the novelty
   multiplier and call `process_stale_exit` are reached but no-op (returning
   1.0 / closing nothing) because the new config sections aren't present.
2. Backup current state files: `runtime_state.json`, `mt5_runtime.json`,
   `trade_journal.db`.

### Phase 1 — generate baselines, test in dry-run (1 day)

1. Run the baseline-fit script:
   ```powershell
   .\.venv\Scripts\python.exe save_novelty_baselines.py
   ```
   Produces `novelty_baselines.json` (~50 KB JSON with mean+inv_cov+percentiles
   per instrument). Verify file exists and per-instrument percentile thresholds
   look sensible (`p70` typically 2-4, `p95` typically 4-7 for 9-feature space).
2. Switch to aggressive config in dry-run, novelty still **disabled**:
   ```powershell
   .\.venv\Scripts\python.exe -u live_trader.py --config mt5_runtime_aggressive.json --dry-run
   ```
   Goal: verify the live trader loads the new config without errors. No real
   orders are sent. BE-trail per-asset configs (ETHUSD/USOIL changed) take
   effect. Risk in dry-run is 5.70% (logged but not executed).
3. Flip `novelty.enabled: true` in `mt5_runtime_aggressive.json`, re-run dry-run.
   Logs should now show `[novelty] loaded baselines for N instruments` and
   per-trade `risk={X}%` will reflect the multiplier when Mahalanobis distance
   is high.

### Phase 2 — go live, conservative risk (1 week)

1. Stop the running live trader (Ctrl+C or `Stop-Process`).
2. Edit `mt5_runtime_aggressive.json`: set `tier.base_risk_pct: 2.0` (revert
   the risk bump for now), keep `novelty.enabled: true`, keep
   `stale_exit.enabled: false`.
3. Start live:
   ```powershell
   .\.venv\Scripts\python.exe -u live_trader.py --config mt5_runtime_aggressive.json
   ```
4. Observe for ≥1 week. Compare to the same period under the conservative
   config (you should see modestly lower risk on outlier-feature bars and
   slightly different position sizing). DD should stay under the conservative
   config's typical range. **No order management semantics change** vs your
   current live deployment beyond the novelty scaling.

### Phase 3 — enable stale-exit (1 week)

1. Stop live.
2. Edit aggressive config: `stale_exit.enabled: true`, keep risk at 2.0%.
3. Start live. Observe positions that approach the stale_bars=72 threshold
   without making +0.75R progress — they should be force-closed at market.
   Verify journal updates.
4. Watch for false positives — trades closed prematurely that would have
   resolved on the next swing. If too aggressive, raise `stale_bars` (96).

### Phase 4 — bump risk to 3.0% (1 week)

1. Stop live.
2. Edit aggressive config: `tier.base_risk_pct: 3.0`. Novelty + stale_exit
   stay enabled.
3. Observe. DD should be modestly higher than Phase 3.

### Phase 5 — bump risk to 5.70% (full V_BASE)

1. Stop live.
2. Edit aggressive config: `tier.base_risk_pct: 5.70`.
3. Observe carefully. Live DD can deviate from backtest DD by 5-10pp from
   regime drift or slippage. If realized DD exceeds 60%, drop back to Phase 4.

## Rollback procedure

At any phase, revert by stopping the trader and starting with the original config:

```powershell
.\.venv\Scripts\python.exe -u live_trader.py --config mt5_runtime.json
```

`runtime_state.json` is shared across configs — open positions are re-tracked
on restart via `auto_adopt_orphan_positions()`.

## What's NOT wired (untested in live)

These were tested in the [reports/improvements.md](reports/improvements.md)
sweep but did not beat V_BASE, so not implemented in live:

- **Per-instrument DD throttle** (V_INST_DD): close to V_BASE but slightly
  lower return. Could be added later as a guardrail; needs persistent
  per-instrument PnL tracking in `runtime_state.json`.
- **Outcome-aware cooldown** (V_COOLDOWN): made things worse — skipped trades
  that recovered. Don't deploy.
- **Expanded novelty features** (V_FEATX): worse than the 9-feature default.
- **Compounding cap** (V_COMP_CAP): mechanical cap — useful as a safety knob
  if you want to lock in gains and stop compounding tail risk, but cuts return
  by ~97% (linear vs geometric growth).

## Maintenance

- **Re-fit baselines weekly**. The Mahalanobis baseline is static; market
  regime drifts. Cron / Task Scheduler:
  ```powershell
  cd C:\Users\Administrator\fx-edge
  .\.venv\Scripts\python.exe save_novelty_baselines.py
  ```
- The live trader does NOT auto-reload the baselines file — restart the
  trader after re-fitting to pick up new baselines.
- The novelty file is small (~50 KB); back up before refitting.

## Known gaps and caveats

1. **Test slice was strong-trend regime** ([reports/DEPLOYMENT.md](reports/DEPLOYMENT.md) Phase 6).
   In an adverse regime, V_BASE return drops; DD may widen.
2. **Single train/test split, no walk-forward validation of the full
   pipeline**. The novelty thresholds were fit once on train data and applied
   to test. Live performance will degrade as the cached train period ages.
   Refitting addresses this slowly.
3. **DD sits 0.08pp under the 60% cap**. A few bad trades early in the new
   regime could push live DD past 60%. The phased rollout starting at 2.0%
   risk gives you headroom while validating each piece.
4. **No alert on DD breach**. Add a daily-loss-halt check to your monitoring
   (production_params.json had `daily_loss_halt_pct: 3.0` — verify it's wired).
