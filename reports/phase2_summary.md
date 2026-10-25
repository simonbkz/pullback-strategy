# Phase 2 — Walk-forward + ML filter + Portfolio

Run: 2026-04-28 17:56:50

## 1. Walk-forward results (out-of-sample only)

| instrument   | asset_class   |   oos_trades |   baseline_return_pct |   oos_return_pct |   oos_max_dd_pct |   rec_swing |   rec_atr_mult |   rec_rr |   rec_pullback_ema |
|--------------|---------------|--------------|-----------------------|------------------|------------------|-------------|----------------|----------|--------------------|
| XAUUSD       | commodity     |           60 |                 32.50 |            21.55 |             7.97 |          25 |           2.00 |     3.00 |                 15 |
| US100        | index         |           44 |                  7.95 |            17.89 |             6.96 |          15 |           2.00 |     3.00 |                 30 |
| JP225        | index         |           51 |                 26.00 |            14.73 |            10.15 |          25 |           1.50 |     2.00 |                 15 |
| USOIL        | commodity     |          155 |                  3.55 |            12.63 |            12.11 |          15 |           1.50 |     2.00 |                 30 |
| CHFJPY       | fx_cross      |          179 |                 13.61 |            12.47 |            11.21 |          25 |           1.50 |     1.50 |                 30 |
| HK50         | index         |           47 |                 13.39 |            11.71 |             4.41 |          25 |           2.00 |     3.00 |                 20 |
| XRPUSD       | crypto        |           77 |                 -5.68 |             7.79 |            13.00 |          25 |           2.00 |     3.00 |                 20 |
| UK100        | index         |           78 |                 -5.09 |             4.35 |            11.14 |          15 |           1.00 |     1.50 |                 20 |
| US500        | index         |           66 |                 26.27 |             4.33 |            10.69 |          20 |           1.00 |     3.00 |                 20 |
| DE40         | index         |           66 |                 22.67 |            -0.61 |            16.77 |          15 |           1.50 |     2.00 |                 15 |
| BTCUSD       | crypto        |          108 |                 -7.47 |            -4.51 |            15.58 |          20 |           2.00 |     3.00 |                 15 |
| US30         | index         |           43 |                  3.21 |            -6.95 |            11.53 |          20 |           2.00 |     2.50 |                 15 |
| EURJPY       | fx_cross      |          199 |                  0.16 |            -7.39 |            19.19 |          15 |           1.00 |     2.00 |                 30 |
| ETHUSD       | crypto        |          148 |                 21.88 |            -8.71 |            11.82 |          15 |           1.50 |     3.00 |                 15 |
| GBPUSD       | fx_major      |          120 |                 -8.12 |           -10.41 |            19.00 |          25 |           1.50 |     3.00 |                 20 |
| SOLUSD       | crypto        |          117 |                 -1.88 |           -13.81 |            20.28 |          25 |           1.50 |     2.50 |                 15 |


## 2. ML filter — held-out test set per instrument

| instrument   |   n_train |   n_test |   kept_pct |   unfilt_trades |   unfilt_WR |   unfilt_avgR |   unfilt_ret |   unfilt_DD |   filt_trades |   filt_WR |   filt_avgR |   filt_ret |   filt_DD |
|--------------|-----------|----------|------------|-----------------|-------------|---------------|--------------|-------------|---------------|-----------|-------------|------------|-----------|
| XAUUSD       |        42 |       18 |       5.56 |              18 |       38.89 |          0.55 |        10.01 |        4.91 |             1 |      0.00 |       -1.00 |      -1.00 |      0.00 |
| DE40         |        46 |       20 |      35.00 |              20 |       10.00 |         -0.68 |       -12.82 |       12.33 |             7 |     14.29 |       -0.50 |      -3.52 |      3.95 |
| US500        |        46 |       20 |      30.00 |              20 |       30.00 |          0.23 |         4.46 |        5.02 |             6 |     33.33 |        0.31 |       1.79 |      2.01 |
| JP225        |        35 |       16 |       6.25 |              16 |       56.25 |          0.72 |        12.01 |        2.00 |             1 |    100.00 |        1.99 |       1.99 |      0.00 |
| ETHUSD       |       103 |       45 |      17.78 |              45 |       22.22 |         -0.20 |        -9.30 |        9.57 |             8 |     50.00 |        0.88 |       7.12 |      2.04 |
| CHFJPY       |       125 |       54 |      31.48 |              54 |       35.19 |          0.09 |         4.24 |        7.95 |            17 |     41.18 |        0.26 |       4.33 |      5.02 |
| SOLUSD       |        81 |       36 |       5.56 |              36 |       25.00 |         -0.23 |        -8.20 |       15.46 |             2 |     50.00 |        0.54 |       1.06 |      0.00 |
| USOIL        |       108 |       47 |      31.91 |              47 |       44.68 |          0.30 |        14.31 |        7.01 |            15 |     66.67 |        0.95 |      15.02 |      3.00 |
| XRPUSD       |        53 |       24 |      25.00 |              24 |       41.67 |          0.62 |        15.49 |        4.99 |             6 |     16.67 |       -0.37 |      -2.24 |      2.04 |
| BTCUSD       |        75 |       33 |       0.00 |              33 |       30.30 |          0.10 |         2.68 |       13.59 |             0 |      0.00 |        0.00 |       0.00 |      0.00 |
| UK100        |        54 |       24 |      29.17 |              24 |       50.00 |          0.48 |        11.95 |        3.96 |             7 |     57.14 |        0.71 |       5.00 |      1.92 |
| EURJPY       |       139 |       60 |      26.67 |              60 |       31.67 |         -0.14 |        -8.63 |       19.19 |            16 |     37.50 |       -0.04 |      -0.83 |      6.98 |
| GBPUSD       |        84 |       36 |      13.89 |              36 |       25.00 |         -0.05 |        -2.39 |        9.01 |             5 |      0.00 |       -1.03 |      -5.06 |      4.07 |


## 3. Portfolio backtests

| scheme                  | params        |   trades_taken |   trades_skipped |   return_pct |   max_dd_pct |
|-------------------------|---------------|----------------|------------------|--------------|--------------|
| baseline_default_params | risk1.0_conc3 |            110 |             3398 |        14.70 |        12.26 |
| baseline_default_params | risk1.0_conc5 |            157 |             3351 |        12.85 |        17.39 |
| baseline_default_params | risk2.0_conc3 |            110 |             3398 |        27.97 |        23.86 |
| wf_optimized            | risk1.0_conc3 |             94 |             1464 |       -10.80 |        16.83 |
| wf_optimized            | risk1.0_conc5 |            149 |             1409 |       -19.33 |        22.50 |
| wf_optimized            | risk2.0_conc3 |             94 |             1464 |       -22.01 |        31.18 |
| wf_plus_ml_filter       | risk1.0_conc3 |             33 |               58 |        -1.11 |         9.91 |
| wf_plus_ml_filter       | risk1.0_conc5 |             75 |               16 |        16.66 |         9.91 |
| wf_plus_ml_filter       | risk2.0_conc3 |             33 |               58 |        -2.79 |        19.09 |


## 4. Recommended MT5 parameters per instrument (from WF)

```json
{
  "BTCUSD": {
    "atr_stop_mult": 2.0,
    "pullback_ema": 15,
    "rr_target": 3.0,
    "swing_lookback": 20
  },
  "CHFJPY": {
    "atr_stop_mult": 1.5,
    "pullback_ema": 30,
    "rr_target": 1.5,
    "swing_lookback": 25
  },
  "DE40": {
    "atr_stop_mult": 1.5,
    "pullback_ema": 15,
    "rr_target": 2.0,
    "swing_lookback": 15
  },
  "ETHUSD": {
    "atr_stop_mult": 1.5,
    "pullback_ema": 15,
    "rr_target": 3.0,
    "swing_lookback": 15
  },
  "EURJPY": {
    "atr_stop_mult": 1.0,
    "pullback_ema": 30,
    "rr_target": 2.0,
    "swing_lookback": 15
  },
  "GBPUSD": {
    "atr_stop_mult": 1.5,
    "pullback_ema": 20,
    "rr_target": 3.0,
    "swing_lookback": 25
  },
  "HK50": {
    "atr_stop_mult": 2.0,
    "pullback_ema": 20,
    "rr_target": 3.0,
    "swing_lookback": 25
  },
  "JP225": {
    "atr_stop_mult": 1.5,
    "pullback_ema": 15,
    "rr_target": 2.0,
    "swing_lookback": 25
  },
  "SOLUSD": {
    "atr_stop_mult": 1.5,
    "pullback_ema": 15,
    "rr_target": 2.5,
    "swing_lookback": 25
  },
  "UK100": {
    "atr_stop_mult": 1.0,
    "pullback_ema": 20,
    "rr_target": 1.5,
    "swing_lookback": 15
  },
  "US100": {
    "atr_stop_mult": 2.0,
    "pullback_ema": 30,
    "rr_target": 3.0,
    "swing_lookback": 15
  },
  "US30": {
    "atr_stop_mult": 2.0,
    "pullback_ema": 15,
    "rr_target": 2.5,
    "swing_lookback": 20
  },
  "US500": {
    "atr_stop_mult": 1.0,
    "pullback_ema": 20,
    "rr_target": 3.0,
    "swing_lookback": 20
  },
  "USOIL": {
    "atr_stop_mult": 1.5,
    "pullback_ema": 30,
    "rr_target": 2.0,
    "swing_lookback": 15
  },
  "XAUUSD": {
    "atr_stop_mult": 2.0,
    "pullback_ema": 15,
    "rr_target": 3.0,
    "swing_lookback": 25
  },
  "XRPUSD": {
    "atr_stop_mult": 2.0,
    "pullback_ema": 20,
    "rr_target": 3.0,
    "swing_lookback": 25
  }
}
```