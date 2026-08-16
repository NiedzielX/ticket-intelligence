# Lech Demand Model v1

This is the first product-oriented historical demand model for Lech Poznań.

## What changes vs v0.3

The prediction target remains Lech home attendance.

The league is used only as a feature factory.

New context:
- closing market-implied probabilities from Football-Data
- opponent historical draw specifically for Lech
- season-specific attendance baseline
- league rest/congestion signals
- opponent and Lech table context
- title reachability / late-season title pressure
- top-four match interaction
- systematic bias calibration

## Validation

Walk-forward test:
- 2022/23
- 2023/24
- 2024/25
- 2025/26

The report separately compares 2024/25 + 2025/26 with the existing v0.3 MAE benchmark of 5,646.3.

## Forecast uncertainty

The model outputs P10 / P50 / P90-like bounds in backtesting.

They are calibrated from chronological residuals rather than pretending the point forecast is exact.

## Important

Do not delete or replace the v0.3 workflow yet.

v1 must beat the existing benchmark before it becomes the historical forecasting engine.
