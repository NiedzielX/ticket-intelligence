# Lech Demand Feature Study v1.2

This is not another blind model-tuning iteration.

The goal is to measure which new information families add real out-of-time
signal for Lech home attendance.

## New feature families

1. Seasonality / calendar
   - cyclical month
   - cyclical day-of-year
   - cyclical kickoff time
   - summer / winter
   - Polish major public-holiday proximity (calculated locally; no extra API)

2. European fixture context
   - days since previous European match
   - days until next European match
   - European matches in prior / next 10 days
   - immediate pre/post-Europe congestion flags

3. Historical weather at Enea Stadion
   - ERA5 reanalysis from Open-Meteo
   - temperature / apparent temperature
   - humidity
   - rain / precipitation
   - cloud cover
   - wind / gusts
   - daily weather

IMPORTANT:
Historical actual weather is used only as an ORACLE feature study.
If it improves the model, the production late forecast must be rebuilt with
historical weather forecasts available at the intended lead time (for example T-7).

4. Market expectation
   - retained only as a late/reference feature family
   - closing market odds are not valid for an early T-30 model

## Candidates

- early_core
- early_plus_seasonality
- early_plus_europe
- early_plus_seasonality_europe
- v11_reference
- late_plus_market
- oracle_plus_weather
- oracle_full

## Evaluation

Walk-forward:
- 2022/23
- 2023/24
- 2024/25
- 2025/26

Holdout:
- 2024/25
- 2025/26

For each candidate:
- MAE
- bias
- interval coverage
- mean interval width
- per-season results

v1.1 holdout benchmark:
- MAE 5567.7

The study deliberately separates EARLY, LATE and ORACLE features so we do not
accidentally build a forecast that uses information unavailable at forecast time.
