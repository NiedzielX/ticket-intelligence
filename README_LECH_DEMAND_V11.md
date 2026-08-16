# Lech Demand Model v1.1

Purpose: fix the two problems found in v1 before adding another external feature family.

Changes:
1. Exclude attendance rows from the pandemic-era capacity-constrained window
   (2020-06-19 through 2021-08-31) from model fitting and attendance rolling history.
2. Preserve the sporting context itself; only the attendance target/history is excluded.
3. Use recency-weighted fitting with a 3-year half-life.
4. Keep walk-forward validation over 2022/23–2025/26.
5. Treat 2022/23 + 2023/24 as development seasons and
   2024/25 + 2025/26 as the holdout benchmark.
6. Promotion requires:
   - holdout MAE lower than v0.3 benchmark (5646.3)
   - P10–P90 interval coverage >= 75%

Do not delete v0.3 or v1 yet.
