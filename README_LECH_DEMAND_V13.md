# Lech Demand Study v1.3

Goal: determine whether richer table-position context improves the early Lech
attendance forecast, while also hardening uncertainty and retesting weather with
a compact feature set.

## What changes

### P50
P50 is now the raw model point forecast.

Calibration no longer shifts P50.

### P10 / P90
Split-conformal calibration is used only to create the uncertainty interval.

### Table importance
New features include:
- leader / top-2 / top-3 status
- opponent top-3 / top-6
- absolute position and points gaps
- close table match
- remaining points available
- normalized gap to leader
- title still mathematically reachable
- close title chase
- late-season leader / chaser interactions
- direct-rival late-season interaction

### Compact weather
Historical actual weather remains ORACLE-only.

Instead of the large v1.2 weather block, only a compact set is tested:
- apparent temperature
- humidity
- rain
- wind
- cloud cover
- discomfort
- bad-conditions flag

Weather should not move to production unless it improves both development and
holdout periods.

## Candidates

1. early_seasonality
2. early_seasonality_table
3. oracle_seasonality_weather_compact
4. oracle_seasonality_table_weather_compact

## Benchmark

v1.2 RAW early + seasonality holdout MAE: 5321.8
