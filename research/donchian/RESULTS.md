# EURUSD Donchian reproducible study

Conclusion: does not meet the preregistered threshold for DEMO forward testing.

100 EUR fixed size; USD 1,000 initial cash per window; all orders simulated. Cash earns zero.
Dukascopy historical bid/ask, not Capital.com CFD quotes; DEMO historical request failed HTTP 404.
Source SHA256: `f7ad8f615eb2dacd9a79417934f99f09a18da32f3c291f37ed605c4db1eda72a`.
Common requested UTC interval [2023-11-01, 2026-11-01). Raw source includes warm-up from 2023-10-12.
Each scored window starts flat and ends flat at a costed last observed close (synthetic boundary valuation).
Initial 40% is unscored training/warm-up. Development aggregates chain the four validation windows only.
The training interval expands; fixed rules require no fit. Warm-up uses only preceding completed bars.
20/10 durations: 30m 10h/5h; primary 1H 20h/10h; 4H 80h/40h, in trading time.
Base: observed spread + 0.1 pip per leg slippage, zero assumed commission, financing unmodeled (zero arithmetic).
High: twice observed spread + 0.5 pip per leg, 5% annualized financing sensitivity.
Financing: base execution + 5% financing. Both sides pay entry midpoint notional / 360 at NY 17:00 weekdays, Wednesday triple.
These are assumptions, not historical Capital.com fees. Midpoint-only fallback would use 1/2 pip base/high full spread.
Gross below is midpoint P&L; costs include spread, slippage, assumed commission and financing. Profit factor uses net trades.
Exposure is calendar time including weekend holding; drawdown is observed open/close equity, not intrabar/tick maximum.

## Exact windows

| Window | Start UTC | End UTC (exclusive) |
|---|---|---|
| validation_1 | 2025-01-12T00:00:00+00:00 | 2025-05-02T00:00:00+00:00 |
| validation_2 | 2025-05-02T00:00:00+00:00 | 2025-08-20T00:00:00+00:00 |
| validation_3 | 2025-08-20T00:00:00+00:00 | 2025-12-07T00:00:00+00:00 |
| validation_4 | 2025-12-07T00:00:00+00:00 | 2026-03-27T00:00:00+00:00 |
| holdout | 2026-03-27T00:00:00+00:00 | 2026-11-01T00:00:00+00:00 |

## Coverage

| Timeframe | Bars | First start UTC | Last end UTC | Incomplete aggregates excluded | Gap events |
|---|---:|---|---|---:|---:|
| HOUR | 18674 | 2023-11-01T00:00:00+00:00 | 2026-10-30T21:00:00+00:00 | 0 | 7 |
| HOUR_4 | 4820 | 2023-11-01T00:00:00+00:00 | 2026-10-31T00:00:00+00:00 | 9 | 7 |
| MINUTE_30 | 37348 | 2023-11-01T00:00:00+00:00 | 2026-10-30T21:00:00+00:00 | 0 | 7 |

Missing active 30m bars: 230; sparse minute-derived 30m candles: 2392; missing minute slots inside observed bars: 5470.
Full missing timestamps and coverage limitations are in study.json. Weekend edges are DST-aware; exceptional holidays are flagged as unexpected gaps. No forward filling.
Aggregates missing an expected 30m constituent are excluded; sparse observed source bars remain, with their actual first observation time. OHLC cannot prove tick completeness.
Unexpected active gaps cancel pending entries, preserve held exits, reset channel warm-up, and carry positions to the next quote; weekend closures retain signals. Missing minute slots within a bar are disclosed but do not reset the channel.

## Primary rules by window, including final holdout

| TF | Window | Cost | Trades | Gross USD | Costs USD | Net USD | Max DD USD | PF | Exposure % |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| HOUR | validation_1 | base | 86 | -1.2205 | 0.6685 | -1.8890 | 3.9990 | 0.8842 | 69.7727 |
| HOUR | validation_2 | base | 84 | -1.9940 | 0.6360 | -2.6300 | 5.3860 | 0.8247 | 60.0758 |
| HOUR | validation_3 | base | 84 | -2.3395 | 0.5395 | -2.8790 | 2.9950 | 0.7456 | 72.2095 |
| HOUR | validation_4 | base | 82 | 0.7770 | 0.4870 | 0.2900 | 2.6210 | 1.0278 | 71.2879 |
| HOUR | validation_1 | high | 86 | -1.2205 | 3.0336 | -4.2541 | 4.8622 | 0.7619 | 69.7727 |
| HOUR | validation_2 | high | 84 | -1.9940 | 3.1025 | -5.0965 | 6.4245 | 0.6860 | 60.0758 |
| HOUR | validation_3 | high | 84 | -2.3395 | 3.0402 | -5.3797 | 5.4867 | 0.5779 | 72.2095 |
| HOUR | validation_4 | high | 82 | 0.7770 | 2.9303 | -2.1533 | 3.9395 | 0.8160 | 71.2879 |
| HOUR | validation_1 | financing | 86 | -1.2205 | 1.8491 | -3.0696 | 4.1242 | 0.8194 | 69.7727 |
| HOUR | validation_2 | financing | 84 | -1.9940 | 1.9625 | -3.9565 | 5.8708 | 0.7444 | 60.0758 |
| HOUR | validation_3 | financing | 84 | -2.3395 | 1.9967 | -4.3362 | 4.4522 | 0.6390 | 72.2095 |
| HOUR | validation_4 | financing | 82 | 0.7770 | 1.9513 | -1.1743 | 3.3840 | 0.8937 | 71.2879 |
| MINUTE_30 | validation_1 | base | 176 | -3.6290 | 1.3360 | -4.9650 | 5.8560 | 0.7991 | 65.0189 |
| MINUTE_30 | validation_2 | base | 172 | -2.5155 | 1.2885 | -3.8040 | 7.5710 | 0.8267 | 71.2879 |
| MINUTE_30 | validation_3 | base | 172 | -3.1925 | 1.0725 | -4.2650 | 6.4670 | 0.7510 | 68.6353 |
| MINUTE_30 | validation_4 | base | 160 | -0.2815 | 1.0125 | -1.2940 | 3.1610 | 0.9188 | 65.9091 |
| MINUTE_30 | validation_1 | high | 176 | -3.6290 | 4.8763 | -8.5053 | 9.3198 | 0.6860 | 65.0189 |
| MINUTE_30 | validation_2 | high | 172 | -2.5155 | 4.9672 | -7.4827 | 9.6014 | 0.6886 | 71.2879 |
| MINUTE_30 | validation_3 | high | 172 | -3.1925 | 4.2937 | -7.4862 | 9.0895 | 0.6060 | 68.6353 |
| MINUTE_30 | validation_4 | high | 160 | -0.2815 | 4.2197 | -4.5012 | 5.4556 | 0.7441 | 65.9091 |
| MINUTE_30 | validation_1 | financing | 176 | -3.6290 | 2.4843 | -6.1133 | 6.9758 | 0.7585 | 65.0189 |
| MINUTE_30 | validation_2 | financing | 172 | -2.5155 | 2.6467 | -5.1622 | 8.2654 | 0.7703 | 71.2879 |
| MINUTE_30 | validation_3 | financing | 172 | -3.1925 | 2.1892 | -5.3817 | 7.3845 | 0.6922 | 68.6353 |
| MINUTE_30 | validation_4 | financing | 160 | -0.2815 | 2.2472 | -2.5287 | 3.7318 | 0.8443 | 65.9091 |
| HOUR_4 | validation_1 | base | 23 | 4.4675 | 0.1715 | 4.2960 | 2.8720 | 1.7693 | 74.3933 |
| HOUR_4 | validation_2 | base | 19 | 0.9750 | 0.1240 | 0.8510 | 3.6340 | 1.1637 | 58.9394 |
| HOUR_4 | validation_3 | base | 24 | -4.7640 | 0.1760 | -4.9400 | 7.7100 | 0.4153 | 77.4083 |
| HOUR_4 | validation_4 | base | 19 | 0.3770 | 0.1000 | 0.2770 | 2.8970 | 1.0757 | 69.0909 |
| HOUR_4 | validation_1 | high | 23 | 4.4675 | 1.7123 | 2.7552 | 3.2057 | 1.4396 | 74.3933 |
| HOUR_4 | validation_2 | high | 19 | 0.9750 | 1.5158 | -0.5408 | 4.1232 | 0.9070 | 58.9394 |
| HOUR_4 | validation_3 | high | 24 | -4.7640 | 1.9018 | -6.6658 | 8.7478 | 0.2726 | 77.4083 |
| HOUR_4 | validation_4 | high | 19 | 0.3770 | 1.4209 | -1.0439 | 3.0812 | 0.7538 | 69.0909 |
| HOUR_4 | validation_1 | financing | 23 | 4.4675 | 1.4028 | 3.0647 | 3.1192 | 1.5047 | 74.3933 |
| HOUR_4 | validation_2 | financing | 19 | 0.9750 | 1.2778 | -0.3028 | 4.0102 | 0.9465 | 58.9394 |
| HOUR_4 | validation_3 | financing | 24 | -4.7640 | 1.5818 | -6.3458 | 8.4883 | 0.2936 | 77.4083 |
| HOUR_4 | validation_4 | financing | 19 | 0.3770 | 1.2069 | -0.8299 | 3.0257 | 0.7978 | 69.0909 |
| HOUR | holdout | base | 174 | -2.2805 | 1.1485 | -3.4290 | 6.0840 | 0.8150 | 71.4422 |
| HOUR | holdout | high | 174 | -2.2805 | 6.1093 | -8.3898 | 10.7013 | 0.6103 | 71.4422 |
| HOUR | holdout | financing | 174 | -2.2805 | 3.9168 | -6.1973 | 8.6638 | 0.6890 | 71.4422 |
| MINUTE_30 | holdout | base | 315 | 5.7460 | 2.0380 | 3.7080 | 3.0390 | 1.1724 | 69.5783 |
| MINUTE_30 | holdout | high | 315 | 5.7460 | 8.3641 | -2.6181 | 5.8817 | 0.8946 | 69.5783 |
| MINUTE_30 | holdout | financing | 315 | 5.7460 | 4.4361 | 1.3099 | 3.6208 | 1.0587 | 69.5783 |
| HOUR_4 | holdout | base | 47 | -5.5645 | 0.2565 | -5.8210 | 8.5940 | 0.5790 | 69.1020 |
| HOUR_4 | holdout | high | 47 | -5.5645 | 3.3095 | -8.8740 | 9.8691 | 0.4237 | 69.1020 |
| HOUR_4 | holdout | financing | 47 | -5.5645 | 2.7710 | -8.3355 | 9.5560 | 0.4438 | 69.1020 |

## All development specifications and comparators

All four validations pooled; no nearby parameter was run on holdout.

| TF | Strategy | Cost | Trades | Gross | Costs | Net | Max DD | PF | Exposure % | Net excluding top 5 winners |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| HOUR | donchian_20_10 | base | 336 | -4.7770 | 2.3310 | -7.1080 | 9.2260 | 0.8661 | 68.3276 | -17.4050 |
| HOUR | donchian_20_10 | high | 336 | -4.7770 | 12.1065 | -16.8835 | 17.4528 | 0.7116 | 68.3276 | -26.9164 |
| HOUR | donchian_20_10 | financing | 336 | -4.7770 | 7.7595 | -12.5365 | 13.5158 | 0.7742 | 68.3276 | -22.6369 |
| HOUR | donchian_15_10 | base | 389 | -5.2335 | 2.7195 | -7.9530 | 10.8880 | 0.8657 | 75.7593 | -18.9320 |
| HOUR | donchian_15_10 | high | 389 | -5.2335 | 13.4570 | -18.6905 | 20.3846 | 0.7132 | 75.7593 | -29.4063 |
| HOUR | donchian_25_10 | base | 296 | -1.3050 | 2.0640 | -3.3690 | 7.3740 | 0.9244 | 61.2187 | -13.4640 |
| HOUR | donchian_25_10 | high | 296 | -1.3050 | 11.0112 | -12.3162 | 13.3021 | 0.7520 | 61.2187 | -22.1480 |
| HOUR | donchian_20_8 | base | 375 | -3.7910 | 2.6340 | -6.4250 | 7.9630 | 0.8827 | 63.5820 | -17.3080 |
| HOUR | donchian_20_8 | high | 375 | -3.7910 | 12.6982 | -16.4892 | 17.5742 | 0.7260 | 63.5820 | -27.1022 |
| HOUR | donchian_20_12 | base | 315 | -8.0980 | 2.2740 | -10.3720 | 11.9670 | 0.8131 | 73.4150 | -21.1710 |
| HOUR | donchian_20_12 | high | 315 | -8.0980 | 12.1114 | -20.2094 | 21.0957 | 0.6692 | 73.4150 | -30.6680 |
| HOUR | hma_16_buy | base | 466 | 4.6915 | 3.8165 | 0.8750 | 7.0090 | 1.0168 | 49.9446 | -6.7100 |
| HOUR | hma_16_buy | high | 466 | 4.6915 | 13.8032 | -9.1117 | 13.9850 | 0.8424 | 49.9446 | -16.5094 |
| HOUR | hma_16_buy | financing | 466 | 4.6915 | 7.1907 | -2.4992 | 8.8505 | 0.9532 | 49.9446 | -9.9944 |
| HOUR | cash | base | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | — | 0.0000 | 0.0000 |
| MINUTE_30 | donchian_20_10 | base | 680 | -9.6185 | 4.7095 | -14.3280 | 16.0020 | 0.8203 | 67.7107 | -23.0770 |
| MINUTE_30 | donchian_20_10 | high | 680 | -9.6185 | 18.3569 | -27.9754 | 29.2146 | 0.6810 | 67.7107 | -36.5304 |
| MINUTE_30 | donchian_20_10 | financing | 680 | -9.6185 | 9.5674 | -19.1859 | 20.6171 | 0.7647 | 67.7107 | -27.8119 |
| MINUTE_30 | donchian_15_10 | base | 770 | -9.3605 | 5.3925 | -14.7530 | 15.8510 | 0.8312 | 75.0664 | -23.6880 |
| MINUTE_30 | donchian_15_10 | high | 770 | -9.3605 | 20.6364 | -29.9969 | 31.0172 | 0.6884 | 75.0664 | -38.7434 |
| MINUTE_30 | donchian_25_10 | base | 616 | -9.4435 | 4.3455 | -13.7890 | 15.8660 | 0.8153 | 61.0573 | -22.0940 |
| MINUTE_30 | donchian_25_10 | high | 616 | -9.4435 | 16.8002 | -26.2437 | 27.6700 | 0.6796 | 61.0573 | -34.3597 |
| MINUTE_30 | donchian_20_8 | base | 754 | -7.6545 | 5.2465 | -12.9010 | 14.5910 | 0.8339 | 60.1129 | -20.3150 |
| MINUTE_30 | donchian_20_8 | high | 754 | -7.6545 | 19.1857 | -26.8402 | 27.8096 | 0.6881 | 60.1129 | -34.1123 |
| MINUTE_30 | donchian_20_12 | base | 624 | -10.2320 | 4.4440 | -14.6760 | 16.4580 | 0.8139 | 73.8522 | -23.1560 |
| MINUTE_30 | donchian_20_12 | high | 624 | -10.2320 | 18.1715 | -28.4035 | 29.6201 | 0.6727 | 73.8522 | -36.7228 |
| MINUTE_30 | hma_16_buy | base | 963 | 3.6635 | 7.6505 | -3.9870 | 9.7010 | 0.9441 | 49.8323 | -10.0350 |
| MINUTE_30 | hma_16_buy | high | 963 | 3.6635 | 24.4772 | -20.8137 | 22.0644 | 0.7477 | 49.8323 | -26.7519 |
| MINUTE_30 | hma_16_buy | financing | 963 | 3.6635 | 11.0487 | -7.3852 | 11.8798 | 0.8989 | 49.8323 | -13.4169 |
| MINUTE_30 | cash | base | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | — | 0.0000 | 0.0000 |
| HOUR_4 | donchian_20_10 | base | 85 | 1.0555 | 0.5715 | 0.4840 | 9.8060 | 1.0211 | 69.9410 | -11.1510 |
| HOUR_4 | donchian_20_10 | high | 85 | 1.0555 | 6.5509 | -5.4954 | 11.2816 | 0.7844 | 69.9410 | -16.5027 |
| HOUR_4 | donchian_20_10 | financing | 85 | 1.0555 | 5.4694 | -4.4139 | 10.8264 | 0.8222 | 69.9410 | -15.4867 |
| HOUR_4 | donchian_15_10 | base | 88 | 8.1205 | 0.5655 | 7.5550 | 7.0510 | 1.3721 | 80.4195 | -4.6690 |
| HOUR_4 | donchian_15_10 | high | 88 | 8.1205 | 7.1585 | 0.9620 | 8.5743 | 1.0425 | 80.4195 | -10.5728 |
| HOUR_4 | donchian_25_10 | base | 75 | 1.1175 | 0.5155 | 0.6020 | 9.2460 | 1.0289 | 65.8979 | -10.5200 |
| HOUR_4 | donchian_25_10 | high | 75 | 1.1175 | 6.0296 | -4.9121 | 11.5532 | 0.7880 | 65.8979 | -15.4052 |
| HOUR_4 | donchian_20_8 | base | 90 | 6.0670 | 0.5880 | 5.4790 | 9.1780 | 1.2562 | 64.7037 | -6.5600 |
| HOUR_4 | donchian_20_8 | high | 90 | 6.0670 | 6.3659 | -0.2989 | 10.0105 | 0.9874 | 64.7037 | -11.8042 |
| HOUR_4 | donchian_20_12 | base | 79 | 0.0365 | 0.4935 | -0.4570 | 10.0050 | 0.9800 | 73.7280 | -12.3720 |
| HOUR_4 | donchian_20_12 | high | 79 | 0.0365 | 6.5642 | -6.5277 | 11.6036 | 0.7444 | 73.7280 | -17.7041 |
| HOUR_4 | hma_16_buy | base | 114 | 9.2480 | 0.7860 | 8.4620 | 5.2500 | 1.3751 | 47.6748 | -3.4930 |
| HOUR_4 | hma_16_buy | high | 114 | 9.2480 | 5.2351 | 4.0129 | 7.4760 | 1.1642 | 47.6748 | -7.5967 |
| HOUR_4 | hma_16_buy | financing | 114 | 9.2480 | 3.7651 | 5.4829 | 6.6495 | 1.2331 | 47.6748 | -6.1927 |
| HOUR_4 | cash | base | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | — | 0.0000 | 0.0000 |

## Holdout comparators

HMA16 BUY/flat uses unchanged ask-close slope rules, adapted to completed bars and this study's common execution model.

| TF | Strategy | Cost | Trades | Gross | Costs | Net | Max DD | PF | Exposure % |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| HOUR | hma_16_buy | base | 222 | 2.5890 | 1.6120 | 0.9770 | 3.1900 | 1.0621 | 46.3470 |
| HOUR | hma_16_buy | high | 222 | 2.5890 | 6.1170 | -3.5280 | 5.9207 | 0.8051 | 46.3470 |
| HOUR | hma_16_buy | financing | 222 | 2.5890 | 3.1730 | -0.5840 | 3.5242 | 0.9639 | 46.3470 |
| HOUR | cash | base | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | — | 0.0000 |
| MINUTE_30 | hma_16_buy | base | 488 | -5.7835 | 3.7895 | -9.5730 | 11.3210 | 0.6781 | 48.5258 |
| MINUTE_30 | hma_16_buy | high | 488 | -5.7835 | 12.1503 | -17.9338 | 19.1718 | 0.4992 | 48.5258 |
| MINUTE_30 | hma_16_buy | financing | 488 | -5.7835 | 5.4328 | -11.2163 | 12.8693 | 0.6357 | 48.5258 |
| MINUTE_30 | cash | base | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | — | 0.0000 |
| HOUR_4 | hma_16_buy | base | 62 | -4.9145 | 0.3605 | -5.2750 | 7.0630 | 0.5474 | 46.0807 |
| HOUR_4 | hma_16_buy | high | 62 | -4.9145 | 2.7071 | -7.6216 | 9.2652 | 0.4049 | 46.0807 |
| HOUR_4 | hma_16_buy | financing | 62 | -4.9145 | 1.9746 | -6.8891 | 8.5657 | 0.4418 | 46.0807 |
| HOUR_4 | cash | base | 0 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | — | 0.0000 |

## Primary 1H P&L concentration

| Sample | Net | Top winner | Top 5 winners | Top 5 / positive P&L % | Net without top 5 | Exit-year P&L |
|---|---:|---:|---:|---:|---:|---|
| development | -7.1080 | 3.5590 | 10.2970 | 22.4048 | -17.4050 | 2025: -7.2550, 2026: 0.1470 |
| holdout | -3.4290 | 1.3260 | 4.9000 | 32.4310 | -8.3290 | 2026: -3.4290 |

## Fixed evaluation criteria

- 3_of_4_validations_positive: FAIL
- development_and_holdout_net_positive: FAIL
- development_and_holdout_PF_above_1: FAIL
- 100_development_30_holdout_trades: PASS
- high_cost_development_and_holdout_positive: FAIL
- most_neighbours_positive: FAIL
- development_positive_without_top5: FAIL

Search extent: 180 development configurations/windows and 21 holdout configurations; attempts.jsonl records every attempt. Freeze precedes the single holdout stage. Weak runs are retained.
All per-window HMA, cash and nearby-setting results are in development.csv; holdout.csv is separate. Each runs/ directory contains exact configuration, metrics, simulated trades and equity CSVs.
Reports give stale boundary quote ages, gap holding events and cancelled entries per run. Read study.json, freeze.json and PLAN.md with these results.
This descriptive backtest does not establish statistical significance or live execution feasibility.
