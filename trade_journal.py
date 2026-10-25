"""SQLite-backed trade journal.

Single source of truth for every trade the live system has touched. Each
trade has an OPEN row (logged when the order fills) that gets UPDATED with
exit info when the position closes. Performance reports and ML retrain
data live downstream from this table.

Schema kept flat (no joins) for simplicity — analytics scripts read with
plain SELECT.
"""
from __future__ import annotations
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

JOURNAL_DB = os.environ.get("FXEDGE_JOURNAL_DB", "trade_journal.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    ticket            INTEGER PRIMARY KEY,
    instrument        TEXT NOT NULL,
    broker_symbol     TEXT,
    side              TEXT,
    volume            REAL,
    entry_ts          TEXT,
    entry_price       REAL,
    sl_initial        REAL,
    sl_planned        REAL,
    sl_at_exit        REAL,
    tp                REAL,
    planned_dist      REAL,
    exit_ts           TEXT,
    exit_price        REAL,
    outcome           TEXT,
    r_multiple_gross  REAL,
    profit_usd        REAL,
    commission_usd    REAL,
    swap_usd          REAL,
    be_armed          INTEGER DEFAULT 0,
    adaptive_tighten  INTEGER DEFAULT 0,
    decay_tighten     INTEGER DEFAULT 0,
    delay_swap_fired  INTEGER DEFAULT 0,
    regime_at_entry   TEXT,
    predicted_regime  TEXT,
    features_at_entry TEXT,
    base_risk_pct     REAL,
    effective_risk_pct REAL,
    notes             TEXT,
    logged_at         TEXT,
    closed_logged_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_instrument ON trades(instrument);
CREATE INDEX IF NOT EXISTS idx_exit_ts    ON trades(exit_ts);
CREATE INDEX IF NOT EXISTS idx_entry_ts   ON trades(entry_ts);
CREATE INDEX IF NOT EXISTS idx_outcome    ON trades(outcome);

CREATE TABLE IF NOT EXISTS journal_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    sig_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    instrument        TEXT NOT NULL,
    side              TEXT,
    entry_price       REAL,
    sl                REAL,
    tp                REAL,
    planned_dist      REAL,
    decision          TEXT,
    skip_reason       TEXT,
    ticket            INTEGER,
    cf_outcome        TEXT,
    cf_exit_price     REAL,
    cf_exit_ts        TEXT,
    cf_r_multiple     REAL,
    cf_bars_to_exit   INTEGER,
    cf_computed_at    TEXT,
    logged_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_ts          ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_decision    ON signals(decision);
CREATE INDEX IF NOT EXISTS idx_signals_skip_reason ON signals(skip_reason);
CREATE INDEX IF NOT EXISTS idx_signals_cf_pending  ON signals(cf_outcome) WHERE cf_outcome IS NULL;
"""


def _conn():
    conn = sqlite3.connect(JOURNAL_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Migrations: columns added after initial schema. SQLite ignores ALTER on
    # an already-present column only via try/except, so guard each one.
    for col, decl in [
        ("decay_tighten", "INTEGER DEFAULT 0"),
        # Per-trade post-mortem (added 2026-10-20): filled on close by
        # live_trader._postmortem_for_ticket. Lets exit-config questions be
        # answered by GROUP BY instead of ad-hoc replay scripts.
        ("mae_R", "REAL"),
        ("mfe_R", "REAL"),
        ("bars_to_mfe", "INTEGER"),
        ("alt_be10_R", "REAL"),      # exit_R if BE-trail were 1.0/0.5 + chandelier 2.0/2.5
        ("alt_be15_R", "REAL"),      # exit_R if BE-trail were 1.5/0.5 + chandelier 2.0/2.5
        ("alt_noch_R", "REAL"),      # exit_R with BE 1.0/0.5 and NO chandelier
        ("postmortem_at", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {decl}")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    return conn


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts_to_str(ts) -> str:
    if ts is None: return ""
    if isinstance(ts, str): return ts
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    return str(ts)


def log_trade_open(
    ticket: int,
    instrument: str,
    broker_symbol: str,
    side: str,
    volume: float,
    entry_ts,
    entry_price: float,
    sl_initial: float,
    sl_planned: float,
    tp: float,
    planned_dist: float,
    regime_at_entry: str = None,
    predicted_regime: str = None,
    features: dict | None = None,
    base_risk_pct: float = None,
    effective_risk_pct: float = None,
    notes: str = "",
):
    """Insert (or upsert) the OPEN row for a trade. Idempotent on ticket."""
    conn = _conn()
    try:
        existing = conn.execute("SELECT ticket FROM trades WHERE ticket=?", (ticket,)).fetchone()
        if existing:
            return  # don't overwrite — once a trade is logged, OPEN data is fixed
        conn.execute("""
            INSERT INTO trades (
                ticket, instrument, broker_symbol, side, volume,
                entry_ts, entry_price, sl_initial, sl_planned, tp, planned_dist,
                regime_at_entry, predicted_regime, features_at_entry,
                base_risk_pct, effective_risk_pct, notes, logged_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            ticket, instrument, broker_symbol, side, volume,
            _ts_to_str(entry_ts), entry_price, sl_initial, sl_planned, tp, planned_dist,
            regime_at_entry, predicted_regime,
            json.dumps(features or {}),
            base_risk_pct, effective_risk_pct, notes, _utc_now(),
        ))
        conn.commit()
    finally:
        conn.close()


def update_postmortem(ticket: int, **fields) -> None:
    """Write post-mortem metrics onto an existing trade row. Unknown keys ignored."""
    allowed = {"mae_R", "mfe_R", "bars_to_mfe", "alt_be10_R", "alt_be15_R", "alt_noch_R"}
    cols = {k: v for k, v in fields.items() if k in allowed}
    if not cols:
        return
    cols["postmortem_at"] = _utc_now()
    sets = ", ".join(f"{k}=?" for k in cols)
    with _conn() as conn:
        conn.execute(f"UPDATE trades SET {sets} WHERE ticket=?", (*cols.values(), int(ticket)))
        conn.commit()


def log_trade_close(
    ticket: int,
    exit_ts,
    exit_price: float,
    outcome: str,
    profit_usd: float,
    commission_usd: float = 0.0,
    swap_usd: float = 0.0,
    sl_at_exit: float = None,
    be_armed: bool = False,
    adaptive_tighten: bool = False,
    delay_swap_fired: bool = False,
):
    """Update the existing trade row with exit info. Computes r_multiple
    automatically from entry/exit/planned_dist if available."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT entry_price, planned_dist, side FROM trades WHERE ticket=?",
            (ticket,),
        ).fetchone()
        r_mult = None
        if row and row["planned_dist"] and row["planned_dist"] > 0:
            entry = row["entry_price"]
            side = row["side"]
            pnl_price = (exit_price - entry) if side == "long" else (entry - exit_price)
            r_mult = pnl_price / row["planned_dist"]

        conn.execute("""
            UPDATE trades SET
                exit_ts=?, exit_price=?, outcome=?, r_multiple_gross=?,
                profit_usd=?, commission_usd=?, swap_usd=?, sl_at_exit=?,
                be_armed=?, adaptive_tighten=?, delay_swap_fired=?,
                closed_logged_at=?
            WHERE ticket=?
        """, (
            _ts_to_str(exit_ts), exit_price, outcome, r_mult,
            profit_usd, commission_usd, swap_usd, sl_at_exit,
            int(bool(be_armed)), int(bool(adaptive_tighten)), int(bool(delay_swap_fired)),
            _utc_now(), ticket,
        ))
        conn.commit()
    finally:
        conn.close()


def update_trade_flag(ticket: int, **flags):
    """Set boolean flags on an open trade (e.g. be_armed=True)."""
    if not flags: return
    cols = ", ".join(f"{k}=?" for k in flags)
    vals = [int(bool(v)) for v in flags.values()] + [ticket]
    conn = _conn()
    try:
        conn.execute(f"UPDATE trades SET {cols} WHERE ticket=?", vals)
        conn.commit()
    finally:
        conn.close()


def trade_exists(ticket: int) -> bool:
    conn = _conn()
    try:
        return conn.execute("SELECT 1 FROM trades WHERE ticket=?", (ticket,)).fetchone() is not None
    finally:
        conn.close()


def get_meta(key: str, default: str = None) -> str | None:
    conn = _conn()
    try:
        row = conn.execute("SELECT value FROM journal_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_meta(key: str, value: str):
    conn = _conn()
    try:
        conn.execute("INSERT OR REPLACE INTO journal_meta(key,value) VALUES (?,?)", (key, value))
        conn.commit()
    finally:
        conn.close()


def all_trades_df():
    """Return all trades as a pandas DataFrame."""
    import pandas as pd
    conn = _conn()
    try:
        df = pd.read_sql("SELECT * FROM trades", conn,
                          parse_dates=["entry_ts", "exit_ts", "logged_at", "closed_logged_at"])
        return df
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Signals table — every signal-considered event (taken or skipped) for
# counterfactual analysis. Skipped signals get cf_* fields filled by
# compute_counterfactuals.py after the fact.
# ---------------------------------------------------------------------------
def log_signal(
    ts,
    instrument: str,
    side: str | None,
    entry_price: float | None,
    sl: float | None,
    tp: float | None,
    planned_dist: float | None,
    decision: str,
    skip_reason: str | None = None,
    ticket: int | None = None,
):
    """Insert a signal-decision row. Decision is 'taken' or 'skipped'.
    skip_reason is a short slug like 'blackout_hour', 'news_FOMC',
    'risk_overshoot', 'heat_cap', 'min_lot_undersize'."""
    conn = _conn()
    try:
        # 2026-10-22 dedupe guard: gates that do not burn the bar (hour filter,
        # auto-exclusion, ...) re-log the same signal every poll, inflating
        # counterfactual n and the t-tests built on it (bad_hour_04UTC was 10
        # rows for 4 signals). One row per (bar, instrument, side, outcome).
        if decision == "skipped":
            dup = conn.execute(
                "SELECT 1 FROM signals WHERE ts=? AND instrument=? AND side IS ? AND decision='skipped' AND skip_reason IS ? LIMIT 1",
                (_ts_to_str(ts), instrument, side, skip_reason)).fetchone()
            if dup:
                return
        conn.execute("""
            INSERT INTO signals
              (ts, instrument, side, entry_price, sl, tp, planned_dist,
               decision, skip_reason, ticket, logged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            _ts_to_str(ts), instrument, side,
            float(entry_price) if entry_price is not None else None,
            float(sl) if sl is not None else None,
            float(tp) if tp is not None else None,
            float(planned_dist) if planned_dist is not None else None,
            decision, skip_reason,
            int(ticket) if ticket is not None else None,
            _utc_now(),
        ))
        conn.commit()
    finally:
        conn.close()


def signals_pending_cf_df():
    """Skipped signals with no counterfactual computed yet."""
    import pandas as pd
    conn = _conn()
    try:
        df = pd.read_sql(
            "SELECT * FROM signals WHERE decision='skipped' "
            "AND cf_outcome IS NULL ORDER BY ts",
            conn, parse_dates=["ts"],
        )
        return df
    finally:
        conn.close()


def update_signal_cf(sig_id: int, outcome: str, exit_price: float,
                      exit_ts, r_multiple: float, bars_to_exit: int):
    conn = _conn()
    try:
        conn.execute("""
            UPDATE signals SET
              cf_outcome=?, cf_exit_price=?, cf_exit_ts=?,
              cf_r_multiple=?, cf_bars_to_exit=?, cf_computed_at=?
            WHERE sig_id=?
        """, (outcome, float(exit_price), _ts_to_str(exit_ts),
              float(r_multiple), int(bars_to_exit), _utc_now(), int(sig_id)))
        conn.commit()
    finally:
        conn.close()


def signals_df(decision: str | None = None):
    """Return all signals (or filtered by decision)."""
    import pandas as pd
    conn = _conn()
    try:
        sql = "SELECT * FROM signals"
        params: tuple = ()
        if decision:
            sql += " WHERE decision=?"
            params = (decision,)
        sql += " ORDER BY ts"
        df = pd.read_sql(sql, conn, params=params,
                          parse_dates=["ts", "cf_exit_ts", "logged_at", "cf_computed_at"])
        return df
    finally:
        conn.close()
