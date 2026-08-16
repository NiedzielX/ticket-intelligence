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


SCRIPT_VERSION = "lech-demand-table-weather-study-v1.3"
print(f"Lech Demand Study: {SCRIPT_VERSION}")

INPUT = Path("lech_demand_artifacts_v12/lech_demand_enriched_v12.csv")
ART = Path("lech_demand_artifacts_v13")
ART.mkdir(exist_ok=True)

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

RECENCY_HALF_LIFE_DAYS = 365.25 * 3.0

# Existing benchmark from v1.2 RAW early + seasonality.
V12_EARLY_SEASONALITY_RAW_HOLDOUT_MAE = 5321.8

CATEGORICAL = ["opponent_key"]

# Same early core as v1.2, but no market and no Europe.
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
    "days_to_nearest_public_holiday",
]

# New v1.3 table-importance layer.
TABLE_IMPORTANCE = [
    "is_leader",
    "is_top2",
    "is_top3",
    "opponent_top3",
    "opponent_top6",

    "abs_position_gap",
    "abs_points_gap",
    "same_table_band",
    "points_close_match",

    "remaining_points_available",
    "normalized_points_to_leader",
    "title_alive",
    "title_chaser_close",

    "leader_late",
    "title_chaser_late",
    "top3_match_late",
    "direct_rival_late",

    "position_gap_late",
    "points_gap_late",
    "opponent_strength_late",
]

# Compact oracle weather only. These features are deliberately few.
COMPACT_WEATHER = [
    "weather_apparent_c",
    "weather_humidity_pct",
    "weather_rain_mm_hour",
    "weather_wind_kmh",
    "weather_cloud_pct",
    "weather_discomfort_index",
    "weather_bad_conditions",
]

CANDIDATES = {
    "early_seasonality": {
        "role": "early",
        "numeric": CORE + SEASONALITY,
    },
    "early_seasonality_table": {
        "role": "early",
        "numeric": CORE + SEASONALITY + TABLE_IMPORTANCE,
    },
    "oracle_seasonality_weather_compact": {
        "role": "oracle_weather",
        "numeric": CORE + SEASONALITY + COMPACT_WEATHER,
    },
    "oracle_seasonality_table_weather_compact": {
        "role": "oracle_weather",
        "numeric": (
            CORE
            + SEASONALITY
            + TABLE_IMPORTANCE
            + COMPACT_WEATHER
        ),
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


def add_features(df):
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
            + [
                "weather_apparent_c",
                "weather_humidity_pct",
                "weather_rain_mm_hour",
                "weather_wind_kmh",
                "weather_cloud_pct",
            ]
        )
    )

    for col in numeric_source:
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col],
                errors="coerce",
            )

    # Attendance history — pre-match only.
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

    df["late_season"] = (
        df["season_progress"]
        .clip(0.0, 1.0)
        ** 2
    )

    remaining = (
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
            / remaining
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

    # --------------------------------------------------------------
    # v1.3 TABLE IMPORTANCE
    # --------------------------------------------------------------

    df["is_leader"] = (
        df["lech_position_before"] == 1
    ).astype(int)

    df["is_top2"] = (
        df["lech_position_before"] <= 2
    ).astype(int)

    df["is_top3"] = (
        df["lech_position_before"] <= 3
    ).astype(int)

    df["opponent_top3"] = (
        df["opponent_position_before"] <= 3
    ).astype(int)

    df["opponent_top6"] = (
        df["opponent_position_before"] <= 6
    ).astype(int)

    df["abs_position_gap"] = (
        df["position_gap"].abs()
    )

    df["abs_points_gap"] = (
        df["points_gap"].abs()
    )

    df["same_table_band"] = (
        df["abs_position_gap"] <= 3
    ).astype(int)

    df["points_close_match"] = (
        df["abs_points_gap"] <= 6
    ).astype(int)

    df["remaining_points_available"] = remaining

    df["normalized_points_to_leader"] = (
        df["points_to_leader"]
        / remaining
    ).clip(lower=0.0, upper=2.0)

    df["title_alive"] = (
        df["points_to_leader"]
        <= remaining
    ).astype(int)

    df["title_chaser_close"] = (
        (df["lech_position_before"] <= 3)
        & (df["points_to_leader"] <= 6)
    ).astype(int)

    df["leader_late"] = (
        df["is_leader"]
        * df["late_season"]
    )

    df["title_chaser_late"] = (
        df["title_chaser_close"]
        * df["late_season"]
    )

    df["top3_match_late"] = (
        (
            (df["lech_position_before"] <= 3)
            & (df["opponent_position_before"] <= 3)
        ).astype(int)
        * df["late_season"]
    )

    df["direct_rival_late"] = (
        (
            (df["abs_position_gap"] <= 3)
            & (df["abs_points_gap"] <= 6)
        ).astype(int)
        * df["late_season"]
    )

    df["position_gap_late"] = (
        df["abs_position_gap"]
        * df["late_season"]
    )

    df["points_gap_late"] = (
        df["abs_points_gap"]
        * df["late_season"]
    )

    df["opponent_strength_late"] = (
        df["opponent_ppg_before"]
        * df["late_season"]
    )

    # --------------------------------------------------------------
    # COMPACT WEATHER ORACLE
    # --------------------------------------------------------------

    df["weather_discomfort_index"] = (
        (df["weather_apparent_c"] - 15.0)
        .abs()
    )

    df["weather_bad_conditions"] = (
        (
            (df["weather_rain_mm_hour"] >= 0.5)
            | (df["weather_wind_kmh"] >= 30.0)
            | (df["weather_apparent_c"] <= 0.0)
            | (df["weather_apparent_c"] >= 28.0)
        )
        .astype(int)
    )

    df["target_residual"] = (
        df["attendance"]
        - df["rolling_5"]
    )

    for col in [
        "days_since_lech_prev_league_match",
        "days_since_opponent_prev_league_match",
    ]:
        df[col] = (
            df[col]
            .fillna(14.0)
            .clip(0, 60)
        )

    return (
        df.dropna(
            subset=[
                "attendance",
                "rolling_5",
                "opponent_draw_ratio",
                "lech_position_before",
                "opponent_position_before",
            ]
        )
        .reset_index(drop=True)
    )


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


def conformal_radius(train, numeric_features, alpha=0.20):
    """
    Chronological split-conformal radius.

    P50 is never shifted. The calibration set is used only to estimate
    uncertainty around the point prediction.
    """
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

    model = build_model(numeric_features)

    model.fit(
        core[CATEGORICAL + numeric_features],
        core["target_residual"],
        model__sample_weight=recency_weights(core),
    )

    cal_pred = (
        cal["rolling_5"].to_numpy()
        + model.predict(
            cal[CATEGORICAL + numeric_features]
        )
    )

    abs_residual = np.abs(
        cal["attendance"].to_numpy()
        - cal_pred
    )

    n = len(abs_residual)

    # Finite-sample split-conformal quantile.
    quantile_level = min(
        1.0,
        math.ceil((n + 1) * (1 - alpha)) / n,
    )

    radius = float(
        np.quantile(
            abs_residual,
            quantile_level,
            method="higher",
        )
    )

    return {
        "radius": radius,
        "calibration_rows": int(n),
        "quantile_level": quantile_level,
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
        subset=required
    ).copy()

    rows = []

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

        interval = conformal_radius(
            train,
            numeric,
            alpha=0.20,
        )

        model = build_model(numeric)

        model.fit(
            train[CATEGORICAL + numeric],
            train["target_residual"],
            model__sample_weight=recency_weights(train),
        )

        p50 = (
            test["rolling_5"].to_numpy()
            + model.predict(
                test[CATEGORICAL + numeric]
            )
        )

        p10 = np.clip(
            p50 - interval["radius"],
            0,
            None,
        )

        p90 = (
            p50 + interval["radius"]
        )

        for pos, (_, row) in enumerate(
            test.iterrows()
        ):
            rows.append(
                {
                    "candidate": name,
                    "role": role,
                    "season": season,
                    "match_date": row["match_date"],
                    "opponent": row["opponent"],
                    "actual": float(row["attendance"]),
                    "p10": float(p10[pos]),
                    "p50": float(p50[pos]),
                    "p90": float(p90[pos]),
                    "interval_hit": int(
                        p10[pos]
                        <= row["attendance"]
                        <= p90[pos]
                    ),
                    "interval_width": float(
                        p90[pos] - p10[pos]
                    ),
                    "conformal_radius": float(
                        interval["radius"]
                    ),
                }
            )

    pred = pd.DataFrame(rows)

    if pred.empty:
        raise RuntimeError(
            f"No predictions for {name}"
        )

    development = pred[
        pred["season"].isin(
            DEVELOPMENT_SEASONS
        )
    ].copy()

    holdout = pred[
        pred["season"].isin(
            HOLDOUT_SEASONS
        )
    ].copy()

    per_season = {}

    for season, group in pred.groupby(
        "season"
    ):
        per_season[season] = {
            "p50": metric_set(
                group["actual"],
                group["p50"],
            ),
            "interval_coverage_pct": round(
                100.0
                * float(
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

    holdout_metrics = metric_set(
        holdout["actual"],
        holdout["p50"],
    )

    development_metrics = metric_set(
        development["actual"],
        development["p50"],
    )

    summary = {
        "candidate": name,
        "role": role,
        "model_rows": int(len(data)),
        "feature_count": int(
            len(CATEGORICAL) + len(numeric)
        ),
        "development_mae": development_metrics["mae"],
        "development_bias": development_metrics["bias"],
        "holdout_mae": holdout_metrics["mae"],
        "holdout_bias": holdout_metrics["bias"],
        "holdout_interval_coverage_pct": round(
            100.0
            * float(
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
        "delta_vs_v12_early_seasonality_raw": round(
            holdout_metrics["mae"]
            - V12_EARLY_SEASONALITY_RAW_HOLDOUT_MAE,
            1,
        ),
        "per_season": per_season,
    }

    return pred, summary


def feature_importance(df, candidate_name):
    spec = CANDIDATES[candidate_name]
    numeric = spec["numeric"]

    data = df.dropna(
        subset=(
            CATEGORICAL
            + numeric
            + [
                "attendance",
                "rolling_5",
                "target_residual",
            ]
        )
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
    if not INPUT.exists():
        raise RuntimeError(
            f"Missing enriched v1.2 dataset: {INPUT}"
        )

    raw = pd.read_csv(INPUT)
    data = add_features(raw)

    all_predictions = []
    summaries = []

    for name, spec in CANDIDATES.items():
        print("")
        print(
            f"Evaluating {name} "
            f"({spec['role']})..."
        )

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

    flat_summary = pd.DataFrame(
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

    early_summary = flat_summary[
        flat_summary["role"] == "early"
    ].sort_values(
        "holdout_mae"
    )

    best_early = str(
        early_summary.iloc[0]["candidate"]
    )

    best_overall = str(
        flat_summary.iloc[0]["candidate"]
    )

    baseline = next(
        s
        for s in summaries
        if s["candidate"]
        == "early_seasonality"
    )

    table_model = next(
        s
        for s in summaries
        if s["candidate"]
        == "early_seasonality_table"
    )

    weather_model = next(
        s
        for s in summaries
        if s["candidate"]
        == "oracle_seasonality_weather_compact"
    )

    table_signal = round(
        table_model["holdout_mae"]
        - baseline["holdout_mae"],
        1,
    )

    weather_signal = round(
        weather_model["holdout_mae"]
        - baseline["holdout_mae"],
        1,
    )

    report = {
        "script_version": SCRIPT_VERSION,
        "dataset_rows": int(len(data)),
        "v12_early_seasonality_raw_holdout_mae": (
            V12_EARLY_SEASONALITY_RAW_HOLDOUT_MAE
        ),
        "candidates": summaries,
        "best_early_candidate": best_early,
        "best_overall_candidate": best_overall,
        "signal_delta_mae": {
            "table_importance_vs_seasonality": table_signal,
            "compact_weather_oracle_vs_seasonality": weather_signal,
        },
        "interpretation": {
            "table_importance": (
                "Negative delta means table-importance features improved "
                "the early forecast."
            ),
            "weather": (
                "Weather remains ORACLE-only. It should be promoted only if "
                "signal is stable on both development and holdout seasons."
            ),
            "uncertainty": (
                "P50 is the model point forecast. Conformal calibration affects "
                "only P10/P90 and never shifts P50."
            ),
        },
    }

    flat_summary.to_csv(
        ART / "v13_summary.csv",
        index=False,
    )

    predictions.to_csv(
        ART / "v13_predictions.csv",
        index=False,
    )

    (
        ART / "v13_report.json"
    ).write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    best_pred = predictions[
        predictions["candidate"]
        == best_early
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
            ART / "v13_best_early_top_errors.csv",
            index=False,
        )
    )

    feature_importance(
        data,
        best_early,
    ).to_csv(
        ART / "v13_best_early_feature_importance.csv",
        index=False,
    )

    print("")
    print("=" * 110)
    print("LECH DEMAND STUDY v1.3")
    print(
        flat_summary[
            [
                "candidate",
                "role",
                "development_mae",
                "holdout_mae",
                "holdout_bias",
                "holdout_interval_coverage_pct",
                "holdout_mean_interval_width",
                "delta_vs_v12_early_seasonality_raw",
            ]
        ].to_string(index=False)
    )
    print("=" * 110)
    print(
        f"Table importance delta MAE: {table_signal:+.1f}"
    )
    print(
        f"Compact weather oracle delta MAE: {weather_signal:+.1f}"
    )
    print(
        f"Best early candidate: {best_early}"
    )
    print("SUCCESS")


if __name__ == "__main__":
    main()
