"""Risk-per-trade sizing with regime + equity-curve awareness.

dynamic_risk = base_risk × regime_multiplier × equity_multiplier × (skip flag)

  regime_multiplier:
    trending: 1.5×    — strategy's home turf, lean in
    neutral:  1.0×    — normal sizing
    chop:     0.0×    — skip the trade entirely (use risk=0 to mean "skip")

  equity_multiplier (anti-martingale guard):
    DD <  15%:  1.00×
    DD < 25%:   0.75×
    DD < 40%:   0.50×
    DD ≥ 40%:   0.25×

The chop-skip is the most impactful lever in the test data because the
pullback strategy bleeds in low-ADX conditions. Reducing rather than
zeroing chop gives a slightly different return/DD point; both variants
are evaluated in the sweep.
"""
from __future__ import annotations


REGIME_MULTIPLIER_DEFAULT = {
    "trending": 1.5,
    "neutral":  1.0,
    "chop":     0.0,   # 0 = skip
}

REGIME_MULTIPLIER_REDUCE = {
    "trending": 1.5,
    "neutral":  1.0,
    "chop":     0.4,   # reduced, not skipped
}


def equity_multiplier(current_eq: float, peak_eq: float) -> float:
    if peak_eq <= 0:
        return 1.0
    dd = (peak_eq - current_eq) / peak_eq
    if dd >= 0.40: return 0.25
    if dd >= 0.25: return 0.50
    if dd >= 0.15: return 0.75
    return 1.0


def dynamic_risk(
    base_risk_pct: float,
    regime: str,
    current_eq: float,
    peak_eq: float,
    use_regime: bool = True,
    use_equity: bool = True,
    chop_skip: bool = True,
    novelty_multiplier: float = 1.0,
) -> float:
    """Returns the actual risk-per-trade %. 0 means skip the trade.

    novelty_multiplier composes multiplicatively after regime + equity.
    Default 1.0 = no change vs prior behavior. Set <1.0 to attenuate risk
    on bars far from the training feature distribution (Mahalanobis novelty).
    """
    risk = base_risk_pct
    if use_regime:
        m = REGIME_MULTIPLIER_DEFAULT if chop_skip else REGIME_MULTIPLIER_REDUCE
        risk *= m.get(regime, 1.0)
    if use_equity:
        risk *= equity_multiplier(current_eq, peak_eq)
    risk *= float(novelty_multiplier)
    return max(0.0, risk)
