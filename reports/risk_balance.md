# Risk-balance experiments

Run: 2026-05-06 13:33:39

**Winner: `V0_baseline` @ risk=4%  =>  +23277.0% / 49.74% DD**


## VA emergency_mult overrides
{'XAUUSD': 1.5, 'ETHUSD': 2.0}


## VB vol-parity weights (1/std(R), clipped 0.3..2.0)

- XAUUSD: 1.195
- US500: 0.873
- DE40: 0.994
- JP225: 0.948
- CHFJPY: 0.926
- HK50: 1.055
- US100: 1.280
- ETHUSD: 0.903
- USOIL: 0.827


## VC gold toxic hours (UTC, train-period)
[1, 2, 3, 5, 6, 7, 8, 10, 12, 14, 16, 18, 19, 20]

XAUUSD test trades: 116 → 37


## Best feasible per variant (DD<50%)

| variant          |   risk |   trades |   return_pct |   max_dd_pct |
|------------------|--------|----------|--------------|--------------|
| V0_baseline      |   4.00 |      521 |     23277.01 |        49.74 |
| VC_gold_blackout |   4.00 |      442 |     11929.93 |        49.94 |
| VB_vol_parity    |   3.00 |      521 |      9375.19 |        40.52 |
| VA_tail_cap      |   3.00 |      530 |      4476.53 |        41.89 |
| VD_combined      |   3.00 |      463 |      3625.94 |        42.62 |


## Full sweep (sorted by return)

| variant          |   risk |   trades |   return_pct |   max_dd_pct |
|------------------|--------|----------|--------------|--------------|
| VB_vol_parity    |   6.00 |      521 |    143065.46 |        66.29 |
| V0_baseline      |   6.00 |      521 |    106718.57 |        66.30 |
| VB_vol_parity    |   5.00 |      521 |     68794.35 |        58.55 |
| V0_baseline      |   5.00 |      521 |     54506.24 |        58.61 |
| VC_gold_blackout |   6.00 |      442 |     46229.76 |        67.74 |
| VA_tail_cap      |   6.00 |      530 |     34374.78 |        72.56 |
| VD_combined      |   6.00 |      463 |     30886.02 |        72.02 |
| VB_vol_parity    |   4.00 |      521 |     27860.12 |        50.12 |
| VC_gold_blackout |   5.00 |      442 |     25468.64 |        58.83 |
| V0_baseline      |   4.00 |      521 |     23277.01 |        49.74 |
| VA_tail_cap      |   5.00 |      530 |     20867.15 |        63.72 |
| VD_combined      |   5.00 |      463 |     17594.81 |        63.58 |
| VC_gold_blackout |   4.00 |      442 |     11929.93 |        49.94 |
| VA_tail_cap      |   4.00 |      530 |     10619.33 |        53.50 |
| VB_vol_parity    |   3.00 |      521 |      9375.19 |        40.52 |
| VD_combined      |   4.00 |      463 |      8653.18 |        53.75 |
| V0_baseline      |   3.00 |      521 |      8217.87 |        39.58 |
| VC_gold_blackout |   3.00 |      442 |      4708.71 |        39.75 |
| VA_tail_cap      |   3.00 |      530 |      4476.53 |        41.89 |
| VD_combined      |   3.00 |      463 |      3625.94 |        42.62 |