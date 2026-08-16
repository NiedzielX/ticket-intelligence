#!/usr/bin/env python3

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, RobustScaler


SCRIPT_VERSION = "lech-demand-feature-study-v1.2"
print(f"Lech Demand Feature Study: {SCRIPT_VERSION}")

ART = Path("lech_demand_artifacts_v12")
ART.mkdir(exist_ok=True)

DATASET = ART / "lech_demand_enriched_v12.csv"

TEST_SEASONS = [
    "2022/2023",
    "2023/2024",
    "2024/2025",
    "2025/2026",
]

DEVELOPMENT_SEASONS = [
    "2022/2023",
    "2023/2024",
]

HOLDOUT_SEASONS = [
    "2024/2025",
    "2025/2026",
]

V11_HOLDOUT_MAE = 5567.7
RECENCY_HALF_LIFE_DAYS = 365.25 * 3.0

CATEGORICAL = ["opponent_key"]

CORE = [
    "month",
    "weekday",
    "kickoff_minutes",
    "is_weekend",

    "rolling_3",
    "rolling_5",
    "rolling_10",
    "season_avg_so_far",
    "recent_attendance_trend",

    "opponent_draw_ratio",
    "opponent_recent_draw_ratio",

    "round_no",
    "matches_remaining_after",
    "season_progress",
    "late_season",

    "lech_position_before",
    "opponent_position_before",
    "position_gap",

    "lech_points_before",
    "opponent_points_before",
    "points_gap",
    "points_to_leader",
    "opponent_points_to_leader",
    "leader_margin_if_lech_first",

    "lech_ppg_before",
    "opponent_ppg_before",
    "ppg_gap",

    "lech_last5_points",
    "opponent_last5_points",
    "form_points_gap",

    "lech_last5_goal_diff",
    "opponent_last5_goal_diff",
    "form_gd_gap",

    "days_since_lech_prev_league_match",
    "days_since_opponent_prev_league_match",
    "lech_matches_last_14d",
    "opponent_matches_last_14d",

    "title_reachability",
    "title_pressure",
    "top4_match",
    "late_top4_match",
]

SEASONALITY = [
    "month_sin",
    "month_cos",
    "doy_sin",
    "doy_cos",
    "kickoff_sin",
    "kickoff_cos",
    "is_summer_month",
    "is_winter_month",
    "is_public_holiday",
    "days_to_nearest_public_holiday",
    "holiday_within_1d",
    "holiday_within_3d",
]

EUROPE = [
    "days_since_europe",
    "days_until_europe",
    "europe_within_3d_before",
    "europe_within_4d_after",
    "europe_matches_prev_10d",
    "europe_matches_next_10d",
    "europe_busy_window",
    "europe_season_active",
]

WEATHER = [
    "weather_temperature_c",
    "weather_apparent_c",
    "weather_humidity_pct",
    "weather_precip_mm_hour",
    "weather_rain_mm_hour",
    "weather_code",
    "weather_cloud_pct",
    "weather_wind_kmh",
    "weather_gust_kmh",
    "weather_is_day",
    "weather_daily_temp_max_c",
    "weather_daily_temp_min_c",
    "weather_daily_temp_range_c",
    "weather_daily_precip_mm",
    "weather_daily_rain_mm",
    "weather_daily_wind_max_kmh",
    "weather_daily_gust_max_kmh",
    "weather_sunshine_hours",
]

MARKET = [
    "market_home_prob",
    "market_draw_prob",
    "market_away_prob",
    "market_expected_lech_points",
    "market_balance",
]

CANDIDATES = {
    "early_core": {
        "role": "early",
        "numeric": CORE,
    },
    "early_plus_seasonality": {
        "role": "early",
        "numeric": CORE + SEASONALITY,
    },
    "early_plus_europe": {
        "role": "early",
        "numeric": CORE + EUROPE,
    },
    "early_plus_seasonality_europe": {
        "role": "early",
        "numeric": CORE + SEASONALITY + EUROPE,
    },
    "v11_reference": {
        "role": "late_reference",
        "numeric": CORE + MARKET,
    },
    "late_plus_market": {
        "role": "late",
        "numeric": CORE + SEASONALITY + EUROPE + MARKET,
    },
    "oracle_plus_weather": {
        "role": "oracle_weather",
        "numeric": CORE + SEASONALITY + EUROPE + WEATHER,
    },
    "oracle_full": {
        "role": "oracle_full",
        "numeric": CORE + SEASONALITY + EUROPE + WEATHER + MARKET,
    },
}


def metric_set(actual, pred):
    actual = np.asarray(actual, dtype=float)
    pred = np.asarray(pred, dtype=float)

    error = pred - actual
    ae = np.abs(error)

    return {
        "mae": round(float(np.mean(ae)), 1),
        "median_ae": round(float(np.median(ae)), 1),
        "rmse": round(
            float(math.sqrt(np.mean(error ** 2))),
            1,
        ),
        "mape_pct": round(
            float(np.mean(ae / actual) * 100),
            2,
        ),
        "bias": round(float(np.mean(error)), 1),
    }


def add_engineered_features(df):
    df = df.copy()

    if "capacity_constrained_for_model" in df.columns:
        constrained = (
            df["capacity_constrained_for_model"]
            .astype(str)
            .str.lower()
            .isin(["true", "1", "yes"])
        )
        print(
            "Excluding capacity-constrained attendance rows: "
            f"{int(constrained.sum())}"
        )
        df = df.loc[~constrained].copy()

    df["match_date"] = pd.to_datetime(
        df["match_date"],
        utc=True,
        errors="raise",
    )

    df = df.sort_values(
        "match_date"
    ).reset_index(drop=True)

    local = df["match_date"].dt.tz_convert(
        "Europe/Warsaw"
    )

    df["month"] = local.dt.month
    df["weekday"] = local.dt.weekday
    df["kickoff_minutes"] = (
        local.dt.hour * 60
        + local.dt.minute
    )
    df["is_weekend"] = (
        df["weekday"].isin([5, 6]).astype(int)
    )

    numeric_source = sorted(
        set(
            [
                "attendance",
                "round_no",
                "matches_remaining_after",
                "season_progress",
                "lech_position_before",
                "opponent_position_before",
                "position_gap",
                "lech_points_before",
                "opponent_points_before",
                "points_gap",
                "points_to_leader",
                "opponent_points_to_leader",
                "leader_margin_if_lech_first",
                "lech_ppg_before",
                "opponent_ppg_before",
                "lech_last5_points",
                "opponent_last5_points",
                "lech_last5_goal_diff",
                "opponent_last5_goal_diff",
                "days_since_lech_prev_league_match",
                "days_since_opponent_prev_league_match",
                "lech_matches_last_14d",
                "opponent_matches_last_14d",
            ]
            + SEASONALITY
            + EUROPE
            + WEATHER
            + [
                "market_home_prob",
                "market_draw_prob",
                "market_away_prob",
            ]
        )
    )

    for col in numeric_source:
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col],
                errors="coerce",
            )

    prior = df["attendance"].shift(1)

    df["rolling_3"] = prior.rolling(
        3,
        min_periods=1,
    ).mean()

    df["rolling_5"] = prior.rolling(
        5,
        min_periods=1,
    ).mean()

    df["rolling_10"] = prior.rolling(
        10,
        min_periods=1,
    ).mean()

    df["global_prev_mean"] = (
        prior.expanding().mean()
    )

    df["season_avg_so_far"] = (
        df.groupby("season")["attendance"]
        .transform(
            lambda s:
                s.shift(1)
                .expanding()
                .mean()
        )
        .fillna(df["global_prev_mean"])
    )

    df["recent_attendance_trend"] = (
        df["rolling_3"]
        / df["rolling_10"].replace(0, np.nan)
    )

    df["opponent_prev_mean"] = (
        df.groupby("opponent_key")["attendance"]
        .transform(
            lambda s:
                s.shift(1)
                .expanding()
                .mean()
        )
        .fillna(df["global_prev_mean"])
    )

    df["opponent_draw_ratio"] = (
        df["opponent_prev_mean"]
        / df["global_prev_mean"].replace(0, np.nan)
    ).clip(0.50, 1.80)

    df["opponent_recent_mean"] = (
        df.groupby("opponent_key")["attendance"]
        .transform(
            lambda s:
                s.shift(1)
                .rolling(
                    3,
                    min_periods=1,
                )
                .mean()
        )
        .fillna(df["opponent_prev_mean"])
    )

    df["opponent_recent_draw_ratio"] = (
        df["opponent_recent_mean"]
        / df["global_prev_mean"].replace(0, np.nan)
    ).clip(0.50, 1.80)

    df["opponent_scaled"] = (
        df["rolling_5"]
        * df["opponent_draw_ratio"]
    )

    df["ppg_gap"] = (
        df["lech_ppg_before"]
        - df["opponent_ppg_before"]
    )

    df["form_points_gap"] = (
        df["lech_last5_points"]
        - df["opponent_last5_points"]
    )

    df["form_gd_gap"] = (
        df["lech_last5_goal_diff"]
        - df["opponent_last5_goal_diff"]
    )

    df["market_expected_lech_points"] = (
        3.0 * df["market_home_prob"]
        + df["market_draw_prob"]
    )

    df["market_balance"] = (
        1.0
        - (
            df["market_home_prob"]
            - df["market_away_prob"]
        ).abs()
    )

    df["late_season"] = (
        df["season_progress"]
        .clip(0.0, 1.0)
        ** 2
    )

    max_available = (
        3.0
        * (
            df["matches_remaining_after"]
            + 1
        )
    ).clip(lower=3.0)

    df["title_reachability"] = (
        1.0
        - (
            df["points_to_leader"]
            / max_available
        )
    ).clip(0.0, 1.0)

    df["title_pressure"] = (
        df["late_season"]
        * df["title_reachability"]
    )

    df["top4_match"] = (
        (
            df["lech_position_before"] <= 4
        )
        & (
            df["opponent_position_before"] <= 4
        )
    ).astype(int)

    df["late_top4_match"] = (
        df["top4_match"]
        * df["late_season"]
    )

    df["target_residual"] = (
        df["attendance"]
        - df["rolling_5"]
    )

    for col in [
        "days_since_lech_prev_league_match",
        "days_since_opponent_prev_league_match",
    ]:
        df[col] = df[col].fillna(14.0).clip(0, 60)

    return df.dropna(
        subset=[
            "attendance",
            "rolling_5",
            "opponent_draw_ratio",
            "lech_position_before",
            "opponent_position_before",
        ]
    ).reset_index(drop=True)


def recency_weights(train):
    dates = pd.to_datetime(
        train["match_date"],
        utc=True,
    )

    age_days = (
        dates.max() - dates
    ).dt.total_seconds() / 86400.0

    return np.power(
        0.5,
        age_days / RECENCY_HALF_LIFE_DAYS,
    )


def build_model(numeric_features):
    preprocessing = ColumnTransformer(
        [
            (
                "cat",
                OneHotEncoder(
                    handle_unknown="ignore",
                    sparse_output=False,
                ),
                CATEGORICAL,
            ),
            (
                "num",
                RobustScaler(),
                numeric_features,
            ),
        ],
        remainder="drop",
    )

    regressor = GradientBoostingRegressor(
        random_state=42,
        n_estimators=220,
        learning_rate=0.025,
        max_depth=2,
        min_samples_leaf=7,
        loss="huber",
    )

    return Pipeline(
        [
            ("features", preprocessing),
            ("model", regressor),
        ]
    )


def calibration_from_train(train, features):
    cal_size = max(
        12,
        min(
            24,
            int(round(len(train) * 0.20)),
        ),
    )

    if len(train) - cal_size < 40:
        cal_size = max(
            8,
            len(train) - 40,
        )

    core = train.iloc[:-cal_size].copy()
    cal = train.iloc[-cal_size:].copy()

    model = build_model(features)

    model.fit(
        core[CATEGORICAL + features],
        core["target_residual"],
        model__sample_weight=recency_weights(core),
    )

    raw = (
        cal["rolling_5"].to_numpy()
        + model.predict(
            cal[CATEGORICAL + features]
        )
    )

    residual = (
        cal["attendance"].to_numpy()
        - raw
    )

    # Robust point calibration: no arbitrary +/-4000 cap.
    bias = float(np.median(residual))

    centered_abs = np.abs(
        residual - bias
    )

    radius80 = float(
        np.quantile(
            centered_abs,
            0.80,
            method="higher",
        )
    )

    return {
        "bias": bias,
        "radius80": radius80,
        "calibration_rows": int(len(cal)),
    }


def evaluate_candidate(df, name, spec):
    numeric = spec["numeric"]
    role = spec["role"]

    required = (
        CATEGORICAL
        + numeric
        + [
            "attendance",
            "rolling_5",
            "target_residual",
            "match_date",
            "season",
            "opponent",
        ]
    )

    data = df.dropna(
        subset=[
            c for c in required
            if c in df.columns
        ]
    ).copy()

    predictions = []

    for season in TEST_SEASONS:
        test = data[
            data["season"] == season
        ].copy()

        if test.empty:
            continue

        train = data[
            data["match_date"]
            < test["match_date"].min()
        ].copy()

        if len(train) < 45:
            continue

        calibration = calibration_from_train(
            train,
            numeric,
        )

        model = build_model(numeric)

        model.fit(
            train[CATEGORICAL + numeric],
            train["target_residual"],
            model__sample_weight=recency_weights(train),
        )

        raw = (
            test["rolling_5"].to_numpy()
            + model.predict(
                test[CATEGORICAL + numeric]
            )
        )

        p50 = raw + calibration["bias"]
        p10 = p50 - calibration["radius80"]
        p90 = p50 + calibration["radius80"]

        for pos, (_, row) in enumerate(
            test.iterrows()
        ):
            predictions.append(
                {
                    "candidate": name,
                    "role": role,
                    "season": season,
                    "match_date": row["match_date"],
                    "opponent": row["opponent"],
                    "actual": float(row["attendance"]),
                    "raw_p50": float(raw[pos]),
                    "p10": float(max(0, p10[pos])),
                    "p50": float(max(0, p50[pos])),
                    "p90": float(max(0, p90[pos])),
                    "interval_hit": int(
                        p10[pos]
                        <= row["attendance"]
                        <= p90[pos]
                    ),
                    "interval_width": float(
                        p90[pos] - p10[pos]
                    ),
                    "calibration_bias": float(
                        calibration["bias"]
                    ),
                }
            )

    pred = pd.DataFrame(predictions)

    if pred.empty:
        raise RuntimeError(
            f"No predictions for candidate {name}"
        )

    holdout = pred[
        pred["season"].isin(HOLDOUT_SEASONS)
    ].copy()

    development = pred[
        pred["season"].isin(DEVELOPMENT_SEASONS)
    ].copy()

    per_season = {}

    for season, group in pred.groupby("season"):
        per_season[season] = {
            "p50": metric_set(
                group["actual"],
                group["p50"],
            ),
            "raw_p50": metric_set(
                group["actual"],
                group["raw_p50"],
            ),
            "interval_coverage_pct": round(
                100 * float(
                    group["interval_hit"].mean()
                ),
                1,
            ),
            "mean_interval_width": round(
                float(
                    group["interval_width"].mean()
                ),
                1,
            ),
        }

    summary = {
        "candidate": name,
        "role": role,
        "model_rows": int(len(data)),
        "feature_count": int(
            len(CATEGORICAL) + len(numeric)
        ),
        "overall_mae": metric_set(
            pred["actual"],
            pred["p50"],
        )["mae"],
        "development_mae": metric_set(
            development["actual"],
            development["p50"],
        )["mae"],
        "holdout_mae": metric_set(
            holdout["actual"],
            holdout["p50"],
        )["mae"],
        "holdout_bias": metric_set(
            holdout["actual"],
            holdout["p50"],
        )["bias"],
        "holdout_interval_coverage_pct": round(
            100 * float(
                holdout["interval_hit"].mean()
            ),
            1,
        ),
        "holdout_mean_interval_width": round(
            float(
                holdout["interval_width"].mean()
            ),
            1,
        ),
        "delta_vs_v11_holdout_mae": round(
            metric_set(
                holdout["actual"],
                holdout["p50"],
            )["mae"]
            - V11_HOLDOUT_MAE,
            1,
        ),
        "per_season": per_season,
    }

    return pred, summary


def final_importance(df, candidate_name):
    spec = CANDIDATES[candidate_name]
    numeric = spec["numeric"]

    data = df.dropna(
        subset=CATEGORICAL + numeric + [
            "attendance",
            "rolling_5",
            "target_residual",
        ]
    ).copy()

    model = build_model(numeric)

    model.fit(
        data[CATEGORICAL + numeric],
        data["target_residual"],
        model__sample_weight=recency_weights(data),
    )

    names = (
        model.named_steps["features"]
        .get_feature_names_out()
    )

    importance = (
        model.named_steps["model"]
        .feature_importances_
    )

    return (
        pd.DataFrame(
            {
                "feature": names,
                "importance": importance,
            }
        )
        .sort_values(
            "importance",
            ascending=False,
        )
        .reset_index(drop=True)
    )


def main():
    if not DATASET.exists():
        raise RuntimeError(
            f"Missing enriched dataset: {DATASET}"
        )

    raw = pd.read_csv(DATASET)
    data = add_engineered_features(raw)

    all_predictions = []
    summaries = []

    for name, spec in CANDIDATES.items():
        print("")
        print(f"Evaluating {name} ({spec['role']})...")

        pred, summary = evaluate_candidate(
            data,
            name,
            spec,
        )

        all_predictions.append(pred)
        summaries.append(summary)

        print(
            f"  holdout MAE={summary['holdout_mae']:.1f}, "
            f"bias={summary['holdout_bias']:.1f}, "
            f"coverage={summary['holdout_interval_coverage_pct']:.1f}%, "
            f"width={summary['holdout_mean_interval_width']:.1f}"
        )

    predictions = pd.concat(
        all_predictions,
        ignore_index=True,
    )

    summary_df = pd.DataFrame(
        [
            {
                k: v
                for k, v in s.items()
                if k != "per_season"
            }
            for s in summaries
        ]
    ).sort_values(
        "holdout_mae"
    )

    early_names = [
        name
        for name, spec in CANDIDATES.items()
        if spec["role"] == "early"
    ]

    early_summary = summary_df[
        summary_df["candidate"].isin(
            early_names
        )
    ].sort_values("holdout_mae")

    best_early = (
        early_summary.iloc[0]["candidate"]
    )

    best_overall = (
        summary_df.iloc[0]["candidate"]
    )

    reference = next(
        s for s in summaries
        if s["candidate"]
        == "early_plus_seasonality_europe"
    )

    weather = next(
        s for s in summaries
        if s["candidate"]
        == "oracle_plus_weather"
    )

    market = next(
        s for s in summaries
        if s["candidate"]
        == "late_plus_market"
    )

    report = {
        "script_version": SCRIPT_VERSION,
        "v11_holdout_mae": V11_HOLDOUT_MAE,
        "dataset_rows": int(len(data)),
        "candidates": summaries,
        "best_early_candidate": best_early,
        "best_overall_candidate": best_overall,
        "incremental_signal": {
            "weather_oracle_mae_delta_vs_early_enriched": round(
                weather["holdout_mae"]
                - reference["holdout_mae"],
                1,
            ),
            "market_mae_delta_vs_early_enriched": round(
                market["holdout_mae"]
                - reference["holdout_mae"],
                1,
            ),
        },
        "interpretation_rules": {
            "weather": (
                "Actual/reanalysis weather is an ORACLE research feature. "
                "If it helps, the next step is a T-7 historical forecast "
                "archive, not production use of actual weather."
            ),
            "market": (
                "Closing market probabilities are a LATE feature and "
                "must not be used in an early T-30 forecast."
            ),
        },
    }

    summary_df.to_csv(
        ART / "feature_study_summary_v12.csv",
        index=False,
    )

    predictions.to_csv(
        ART / "feature_study_predictions_v12.csv",
        index=False,
    )

    (
        ART / "feature_study_report_v12.json"
    ).write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # Error table for the best early candidate.
    best_pred = predictions[
        predictions["candidate"] == best_early
    ].copy()

    best_pred["error"] = (
        best_pred["p50"]
        - best_pred["actual"]
    )
    best_pred["abs_error"] = (
        best_pred["error"].abs()
    )

    (
        best_pred.sort_values(
            "abs_error",
            ascending=False,
        )
        .head(20)
        .to_csv(
            ART / "best_early_top_errors_v12.csv",
            index=False,
        )
    )

    final_importance(
        data,
        best_early,
    ).to_csv(
        ART / "feature_importance_best_early_v12.csv",
        index=False,
    )

    final_importance(
        data,
        best_overall,
    ).to_csv(
        ART / "feature_importance_best_overall_v12.csv",
        index=False,
    )

    print("")
    print("=" * 100)
    print("LECH DEMAND FEATURE STUDY v1.2")
    print(summary_df[
        [
            "candidate",
            "role",
            "holdout_mae",
            "holdout_bias",
            "holdout_interval_coverage_pct",
            "holdout_mean_interval_width",
        ]
    ].to_string(index=False))
    print("=" * 100)
    print(f"Best early candidate:   {best_early}")
    print(f"Best overall candidate: {best_overall}")
    print("SUCCESS")


if __name__ == "__main__":
    main()
