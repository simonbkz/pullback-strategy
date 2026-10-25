# pullback-strategy (fx-edge)

Multi-asset trend-pullback trading system running live on MetaTrader 5 via the
Python `MetaTrader5` API. Signals on H1 bars with an H4 trend filter; per-instrument
strategy selection (pullback / Donchian breakout / momentum); a layered exit stack
(planned stop, break-even trail, chandelier, time-decay stop, early-adverse cut,
stale exit, safety-net take-profit, scale-in on winners); and journal-driven risk
throttles with a statistical gate (one-sample t-test, p <= 0.10) before any pause,
promotion or filter change.

## Layout

| Path | Purpose |
|---|---|
| `live_trader.py` | Live engine: polling loop, entry gates, exit stack, risk throttles, journal |
| `live_mt5_connector.py` | MT5 attach/validate, symbol resolver, order/modify with tick-size rounding, broker-offset detection |
| `strategy.py`, `strategy_breakout.py`, `strategy_momentum.py` | Signal generators |
| `trade_journal.py` | SQLite journal (`trades`, `signals`) incl. counterfactuals and post-mortems |
| `compute_counterfactuals.py` | Scores every skipped signal as if taken (what did each filter cost?) |
| `watchdog.py`, `trade_monitor.py`, `health_pinger.py`, `signal_path_monitor.py`, `regime_pulse.py` | Monitoring stack |
| `broader_walk_forward.py` | Weekly shadow-promotion of candidate instruments |
| `agent_audit.ps1`, `agent_audit/AUDIT_PROMPT.md`, `scripts/audit_checks.py` | Daily read-only LLM audit of the live system |
| `config_lint.py`, `preflight.py` | Config validation before restart |
| `scripts/` | One-off migrations and research scripts (each is dated and idempotent) |
| `reports/` | Research history: every sweep, its winner, and the markdown write-up |
| `mt5_runtime.example.json` | The live config, sanitised (account number and webhook replaced) |
| `config_variants/` | Historical sweep winners (max / optimal / aggressive / protected), sanitised |
| `STRATEGY_CLONE.md` | Step-by-step guide to deploying on a new MT5 account |
| `LIVE_DEPLOYMENT_NOTES.md` | Operational notes |

## Running

1. Install the MT5 terminal, log in, keep it running.
2. `python -m venv .venv && .venv\Scripts\pip install -r requirements.txt MetaTrader5 scipy scikit-learn joblib`
3. Copy `mt5_runtime.example.json` to `mt5_runtime.json`; set `connection.path`,
   `connection.expected_server`, `connection.expected_login` and (optionally)
   `external_monitor.webhook_url`.
4. `python config_lint.py mt5_runtime.json` then `python live_trader.py`.
5. Register the scheduled tasks in `_install_scheduled_tasks.ps1` (watchdog, monitors, counterfactuals, audit).

The config is read once at startup. Every config change needs lint + restart.
Throttles read the journal database live.

## What is deliberately not in the repository

The trade journal, logs, backups, market-data caches, the live `mt5_runtime.json`
and the daily generated reports. See `.gitignore`. Before committing a config change
run `scripts/sanitise_config_for_git.py` to refresh the sanitised copies.

## Research record

`reports/*.md` document each experiment and its verdict, including the negative
results (partial take-profits, mean-reversion ensemble, cross-asset expansion,
alternative trend indicators, S/R stop placement, FX cross expansion, per-instrument
sizing). Check there before re-testing an idea.
