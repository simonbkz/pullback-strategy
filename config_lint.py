"""Static conflict linter for mt5_runtime.json.

Catches the class of silent bugs that let MSC override ensemble on ETHUSD
for 4 days without any visible error. Runs read-only — reports findings as
severity-tagged lines. Exit code non-zero if any ERROR-level finding.

Usage:
  python config_lint.py            # lint the live mt5_runtime.json
  python config_lint.py --file X   # lint a specific file (dry-run config)

Categories checked:
  1. Logical conflicts (mutually-contradictory filters)
  2. Dangling references (strategies/instruments pointing to nothing)
  3. Dead/retired fields (_prev_* clutter, disabled mechanism blocks)
  4. Risk-sizing sanity (overrides outside sane ranges)
  5. Scheduling conflicts (overlapping tasks)
  6. Instrument coverage gaps (symbol_map, be_trail, etc.)

Low effort to maintain: just add check functions. Each returns a list of
findings as (severity, message) tuples.
"""
import json, sys
from pathlib import Path

SEV_ERROR = "ERROR"
SEV_WARN = "WARN"
SEV_INFO = "INFO"


def load(path):
    return json.load(open(path, encoding="utf-8"))


# ---- Checks ----
def check_msc_vs_ensemble(cfg):
    """MSC per_instrument.<INS>=True conflicts with strategy_per_instrument.<INS>='ensemble'."""
    out = []
    msc = cfg.get("multi_signal_confirmation", {})
    if not msc.get("enabled", False):
        return out
    msc_pi = msc.get("per_instrument", {}) or {}
    sp = cfg.get("strategy_per_instrument", {}) or {}
    for ins, is_on in msc_pi.items():
        if ins.startswith("_"): continue
        if not is_on: continue
        strat = sp.get(ins, "pullback")
        if strat.startswith("_"): continue
        if strat == "ensemble":
            out.append((SEV_ERROR,
                f"[conflict] {ins}: msc.per_instrument=True AND strategy_per_instrument='ensemble'. "
                f"MSC bypasses the strategy module → ensemble is silently ignored (2026-10-13 bug class)."))
        elif strat in ("breakout", "momentum"):
            out.append((SEV_WARN,
                f"[conflict] {ins}: msc.per_instrument=True AND strategy_per_instrument='{strat}'. "
                f"MSC requires 2/3 agreement, but '{strat}' is a specific single strategy. "
                f"Signals filtered to the subset where '{strat}' agrees with 1+ other strategies."))
    return out


def check_disabled_vs_strategy(cfg):
    """Instrument explicitly disabled in tier_overrides shouldn't have strategy_per_instrument entry."""
    out = []
    to = cfg.get("tier_overrides", {}) or {}
    sp = cfg.get("strategy_per_instrument", {}) or {}
    for ins, v in to.items():
        if ins.startswith("_"): continue
        if isinstance(v, dict) and v.get("enabled") is False:
            if ins in sp and not sp[ins].startswith("_"):
                out.append((SEV_WARN,
                    f"[dead-config] {ins}: disabled via tier_overrides but still has "
                    f"strategy_per_instrument='{sp[ins]}'. Harmless but misleading."))
    return out


def check_strategy_names(cfg):
    """strategy_per_instrument values must be one of: pullback, breakout, momentum, ensemble."""
    out = []
    VALID = {"pullback", "breakout", "momentum", "ensemble"}
    sp = cfg.get("strategy_per_instrument", {}) or {}
    for ins, strat in sp.items():
        if ins.startswith("_"): continue
        if isinstance(strat, str) and strat.startswith("_"): continue
        if not isinstance(strat, str) or strat not in VALID:
            out.append((SEV_ERROR,
                f"[invalid] strategy_per_instrument.{ins}='{strat}' — must be one of {VALID}"))
    return out


def check_instrument_in_universe(cfg):
    """Every strategy_per_instrument + tier_override + be_trail.per_instrument instrument
       should be in the top-level instruments list."""
    out = []
    universe = set(cfg.get("instruments", []))
    for section in ("strategy_per_instrument", "tier_overrides"):
        d = cfg.get(section, {}) or {}
        for ins in d:
            if ins.startswith("_"): continue
            if ins not in universe:
                out.append((SEV_WARN,
                    f"[orphan] {section}.{ins} references instrument NOT in top-level instruments list. "
                    f"Config will never apply."))
    bt = cfg.get("be_trail", {}).get("per_instrument", {}) or {}
    for ins in bt:
        if ins.startswith("_"): continue
        if ins not in universe:
            out.append((SEV_INFO,
                f"[orphan] be_trail.per_instrument.{ins} not in instruments list."))
    return out


def check_symbol_map_coverage(cfg):
    """Every instrument needs a symbol_map entry so broker resolution works."""
    out = []
    universe = set(cfg.get("instruments", []))
    sm = cfg.get("symbol_map", {}) or {}
    for ins in universe:
        if ins not in sm:
            out.append((SEV_WARN, f"[coverage] {ins} missing from symbol_map."))
    return out


def check_dead_prev_fields(cfg):
    """Count _prev_* clutter."""
    out = []
    def walk(d, path=""):
        count = 0
        if isinstance(d, dict):
            for k, v in d.items():
                if k.startswith("_prev_") or k.startswith("_rollback_"):
                    count += 1
                count += walk(v, f"{path}.{k}")
        elif isinstance(d, list):
            for i, v in enumerate(d):
                count += walk(v, f"{path}[{i}]")
        return count
    n = walk(cfg)
    if n >= 5:
        out.append((SEV_INFO, f"[cleanup] {n} '_prev_*' or '_rollback_*' keys scattered in config. "
                               f"Consider pruning or moving to a separate archive file."))
    return out


def check_risk_sanity(cfg):
    """Per-instrument base_risk_pct overrides should be in sane range (0.1% – 2× global)."""
    out = []
    global_base = float(cfg.get("tier", {}).get("base_risk_pct", 6.0))
    sane_max = global_base * 2.0
    sane_min = 0.1
    to = cfg.get("tier_overrides", {}) or {}
    for ins, v in to.items():
        if ins.startswith("_"): continue
        if not isinstance(v, dict): continue
        rp = v.get("base_risk_pct")
        if rp is None: continue
        try: rp = float(rp)
        except Exception: continue
        if rp < sane_min:
            out.append((SEV_WARN, f"[risk] {ins}: base_risk_pct={rp} < {sane_min}% — "
                                   f"may produce under-minlot sizing."))
        elif rp > sane_max:
            out.append((SEV_ERROR, f"[risk] {ins}: base_risk_pct={rp}% > 2× global ({sane_max}%). "
                                    f"Verify this is intentional."))
    return out


def check_retired_mechanisms(cfg):
    """Flag any mechanism block that's disabled but still present with substantive config."""
    out = []
    for name in ("multi_signal_confirmation", "novelty", "V_INST_DD"):
        blk = cfg.get(name)
        if isinstance(blk, dict) and blk.get("enabled") is False:
            keys = [k for k in blk if not k.startswith("_") and k != "enabled"]
            if keys:
                out.append((SEV_INFO,
                    f"[retired] '{name}' is enabled=false but still has active-looking fields "
                    f"{keys[:5]}. Consider moving to a disabled-archive section."))
    return out


def check_tier_overrides_atr_vs_global(cfg):
    """Flag per-instrument atr_stop_mult overrides outside sane range."""
    out = []
    tier = cfg.get("tier", {}) or {}
    g_atr = float(tier.get("atr_stop_mult", 2.0))
    to = cfg.get("tier_overrides", {}) or {}
    for ins, v in to.items():
        if ins.startswith("_"): continue
        if not isinstance(v, dict): continue
        a = v.get("atr_stop_mult")
        if a is None: continue
        try: a = float(a)
        except Exception: continue
        if a < 0.5 or a > 10.0:
            out.append((SEV_WARN, f"[atr] {ins}: atr_stop_mult={a} outside sane [0.5, 10] range (global {g_atr})."))
    return out


def check_scale_in_vs_be_trail(cfg):
    """Scale-in trigger should be <= BE-trail trigger (otherwise scale-in fires AFTER BE lock)."""
    out = []
    si = cfg.get("scale_in", {}) or {}
    bt = cfg.get("be_trail", {}) or {}
    if not si.get("enabled", False):
        return out
    si_trig = float(si.get("trigger_R", 1.0))
    bt_trig = float(bt.get("default_trigger_R", 1.0))
    if si_trig > bt_trig:
        out.append((SEV_WARN,
            f"[ordering] scale_in.trigger_R={si_trig} > be_trail.default_trigger_R={bt_trig}. "
            f"BE-trail will lock before scale-in fires on most trades. Consider si_trig <= bt_trig."))
    return out


def check_hour_dow_overlap(cfg):
    """Hour filter + DOW filter should not block > 50% of trading time together."""
    out = []
    hf = cfg.get("hour_filter", {}) or {}
    df = cfg.get("dow_filter", {}) or {}
    hours_blocked = 0
    if hf.get("enabled", False):
        hours_blocked = len(hf.get("blocked_hours_utc", []))
    days_blocked = 0
    if df.get("enabled", False):
        days_blocked = len(df.get("blocked_days", []))
    pct = hours_blocked / 24 * 100 + (days_blocked / 7 * 100) * (1 - hours_blocked / 24)
    if pct > 50:
        out.append((SEV_WARN, f"[coverage] Hour+DOW filters block ~{pct:.0f}% of calendar time."))
    elif pct > 35:
        out.append((SEV_INFO, f"[coverage] Hour+DOW filters block ~{pct:.0f}% of calendar time."))
    return out


ALL_CHECKS = [
    check_msc_vs_ensemble,
    check_disabled_vs_strategy,
    check_strategy_names,
    check_instrument_in_universe,
    check_symbol_map_coverage,
    check_dead_prev_fields,
    check_risk_sanity,
    check_retired_mechanisms,
    check_tier_overrides_atr_vs_global,
    check_scale_in_vs_be_trail,
    check_hour_dow_overlap,
]


def main():
    path = "mt5_runtime.json"
    if "--file" in sys.argv:
        i = sys.argv.index("--file")
        if i + 1 < len(sys.argv): path = sys.argv[i + 1]
    cfg = load(path)
    print("=" * 90)
    print(f"CONFIG LINT: {path}")
    print("=" * 90)
    all_findings = []
    for check in ALL_CHECKS:
        all_findings.extend(check(cfg))
    if not all_findings:
        print("\n  OK — no findings.")
        sys.exit(0)
    by_sev = {SEV_ERROR: [], SEV_WARN: [], SEV_INFO: []}
    for sev, msg in all_findings:
        by_sev[sev].append(msg)
    for sev in (SEV_ERROR, SEV_WARN, SEV_INFO):
        if by_sev[sev]:
            print(f"\n{sev} ({len(by_sev[sev])}):")
            for m in by_sev[sev]:
                print(f"  {m}")
    print()
    if by_sev[SEV_ERROR]:
        print(f"FAIL — {len(by_sev[SEV_ERROR])} ERROR finding(s). Fix before deploy.")
        sys.exit(1)
    print(f"PASS — {len(by_sev[SEV_WARN])} warning(s), {len(by_sev[SEV_INFO])} info note(s).")
    sys.exit(0)


if __name__ == "__main__":
    main()
