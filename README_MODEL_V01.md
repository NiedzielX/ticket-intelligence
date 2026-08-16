# Attendance Model A v0.1

## What it does

- reads Lech historical home attendance from Supabase;
- excludes `restricted_capacity = true`;
- uses only pre-match information;
- builds lagged / rolling attendance features without target leakage;
- performs chronological season-based backtesting;
- compares:
  - expanding historical mean,
  - rolling last 5,
  - historical opponent mean,
  - Model A v0.1;
- trains a final model on all unrestricted historical data;
- forecasts Lech vs Raków on 2026-09-06 17:30.

## Files produced

GitHub artifact `attendance-model-v01` contains:

- `model_report.json`
- `backtest_predictions.csv`
- `attendance_model.joblib`

## Important

This is a v0.1 benchmark model, not the final product.
The goal is first to prove that a learned model can outperform simple attendance baselines.
Live Roboticket inventory is intentionally NOT included yet.
