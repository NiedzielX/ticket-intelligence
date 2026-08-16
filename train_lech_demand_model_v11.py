#!/usr/bin/env python3

import json
import math
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, RobustScaler


SCRIPT_VERSION = "lech-demand-model-v1.1"
print(f"Lech Demand Model: {SCRIPT_VERSION}")

ART = Path("lech_demand_artifacts_v1")
DATASET = ART / "lech_demand_dataset_v1.csv"

RECENCY_HALF_LIFE_DAYS = 365.25 * 3.0
DEVELOPMENT_SEASONS = ["2022/2023", "2023/2024"]
HOLDOUT_SEASONS = ["2024/2025", "2025/2026"]

V03_LAST2_MAE = 5646.3

TEST_SEASONS = [
    "2022/2023",
    "2023/2024",
    "2024/2025",
    "2025/2026",
]


CATEGORICAL = [
    "opponent_key",
]

NUMERIC = [
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

    "market_home_prob",
    "market_draw_prob",
    "market_away_prob",
    "market_expected_lech_points",
    "market_balance",

    "title_reachability",
    "title_pressure",
    "top4_match",
    "late_top4_match",
]

FEATURES = CATEGORICAL + NUMERIC


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

    numeric_source = [
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
        "market_home_prob",
        "market_draw_prob",
        "market_away_prob",
    ]

    for col in numeric_source:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce",
        )

    prior = df["attendance"].shift(1)

    df["rolling_3"] = (
        prior.rolling(
            3,
            min_periods=1,
        ).mean()
    )

    df["rolling_5"] = (
        prior.rolling(
            5,
            min_periods=1,
        ).mean()
    )

    df["rolling_10"] = (
        prior.rolling(
            10,
            min_periods=1,
        ).mean()
    )

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

    # Recent fixture-specific draw: only previous Lech home meetings.
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

    # Fill rest-day unknowns only at the start of a season.
    for col in [
        "days_since_lech_prev_league_match",
        "days_since_opponent_prev_league_match",
    ]:
        df[col] = df[col].fillna(14.0).clip(0, 60)

    # Keep only rows with meaningful pre-match table and market data.
    return df.dropna(
        subset=[
            "attendance",
            "rolling_5",
            "opponent_draw_ratio",
            "lech_position_before",
            "opponent_position_before",
            "market_home_prob",
            "market_draw_prob",
            "market_away_prob",
        ]
    ).reset_index(drop=True)


def build_model():
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
                NUMERIC,
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


def calibration_from_train(train):
    # Last ~20% of the available training history is used only to estimate
    # systematic bias and uncertainty. Then the final fold model is refit
    # on all training data.
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

    model = build_model()

    model.fit(
        core[FEATURES],
        core["target_residual"],
        model__sample_weight=recency_weights(core),
    )

    cal_pred = (
        cal["rolling_5"].to_numpy()
        + model.predict(cal[FEATURES])
    )

    residual = (
        cal["attendance"].to_numpy()
        - cal_pred
    )

    # Mean bias correction is intentionally capped to avoid one small
    # calibration window shifting the whole forecast too aggressively.
    bias = float(
        np.clip(
            np.mean(residual),
            -4000,
            4000,
        )
    )

    centered = residual - bias

    q10 = float(np.quantile(centered, 0.10))
    q90 = float(np.quantile(centered, 0.90))

    return {
        "bias": bias,
        "q10": q10,
        "q90": q90,
        "calibration_rows": int(len(cal)),
    }


def predict_fold(train, test):
    calibration = calibration_from_train(train)

    model = build_model()

    model.fit(
        train[FEATURES],
        train["target_residual"],
        model__sample_weight=recency_weights(train),
    )

    raw = (
        test["rolling_5"].to_numpy()
        + model.predict(test[FEATURES])
    )

    p50 = raw + calibration["bias"]
    p10 = p50 + calibration["q10"]
    p90 = p50 + calibration["q90"]

    # Stadium-independent sanity floor/ceiling. These are not stadium
    # capacity assumptions; just guards against pathological extrapolation.
    p10 = np.clip(p10, 0, 60000)
    p50 = np.clip(p50, 0, 60000)
    p90 = np.clip(p90, 0, 60000)

    lo = np.minimum(p10, p90)
    hi = np.maximum(p10, p90)

    return model, calibration, lo, p50, hi


def backtest(df):
    rows = []

    for season in TEST_SEASONS:
        test = df[
            df["season"] == season
        ].copy()

        if test.empty:
            continue

        train = df[
            df["match_date"]
            < test["match_date"].min()
        ].copy()

        if len(train) < 50:
            print(
                f"Skipping {season}: "
                f"only {len(train)} training rows"
            )
            continue

        print(
            f"{season}: "
            f"train={len(train)}, test={len(test)}"
        )

        (
            _model,
            calibration,
            p10,
            p50,
            p90,
        ) = predict_fold(
            train,
            test,
        )

        baseline = test[
            "rolling_5"
        ].to_numpy()

        opponent_scaled = test[
            "opponent_scaled"
        ].to_numpy()

        for pos, (_, row) in enumerate(
            test.iterrows()
        ):
            rows.append(
                {
                    "season": season,
                    "match_date": row["match_date"],
                    "opponent": row["opponent"],
                    "actual": float(
                        row["attendance"]
                    ),
                    "rolling_5": float(
                        baseline[pos]
                    ),
                    "opponent_scaled": float(
                        opponent_scaled[pos]
                    ),
                    "p10": float(p10[pos]),
                    "p50": float(p50[pos]),
                    "p90": float(p90[pos]),
                    "interval_hit": int(
                        p10[pos]
                        <= row["attendance"]
                        <= p90[pos]
                    ),
                    "calibration_bias": float(
                        calibration["bias"]
                    ),
                }
            )

    pred = pd.DataFrame(rows)

    if pred.empty:
        raise RuntimeError(
            "No walk-forward predictions produced."
        )

    overall = {
        col: metric_set(
            pred["actual"],
            pred[col],
        )
        for col in [
            "rolling_5",
            "opponent_scaled",
            "p50",
        ]
    }

    per_season = {}

    for season, group in pred.groupby(
        "season"
    ):
        per_season[season] = {
            col: metric_set(
                group["actual"],
                group[col],
            )
            for col in [
                "rolling_5",
                "opponent_scaled",
                "p50",
            ]
        }

        per_season[season][
            "interval_80_coverage_pct"
        ] = round(
            float(
                group["interval_hit"].mean()
                * 100
            ),
            1,
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

    development_metrics = {
        "p50": metric_set(
            development["actual"],
            development["p50"],
        ),
        "interval_80_coverage_pct": round(
            float(
                development["interval_hit"].mean()
                * 100
            ),
            1,
        ),
    }

    last2_metrics = {
        "p50": metric_set(
            holdout["actual"],
            holdout["p50"],
        ),
        "rolling_5": metric_set(
            holdout["actual"],
            holdout["rolling_5"],
        ),
        "opponent_scaled": metric_set(
            holdout["actual"],
            holdout["opponent_scaled"],
        ),
        "interval_80_coverage_pct": round(
            float(
                holdout["interval_hit"].mean()
                * 100
            ),
            1,
        ),
    }

    pred["model_error"] = (
        pred["p50"]
        - pred["actual"]
    )

    pred["model_abs_error"] = (
        pred["model_error"].abs()
    )

    top_errors = (
        pred.sort_values(
            "model_abs_error",
            ascending=False,
        )
        .head(15)
        .copy()
    )

    return (
        pred,
        overall,
        per_season,
        development_metrics,
        last2_metrics,
        top_errors,
    )


def feature_importance(model):
    pre = model.named_steps["features"]
    reg = model.named_steps["model"]

    names = pre.get_feature_names_out()
    importance = reg.feature_importances_

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
            f"Missing dataset: {DATASET}"
        )

    raw = pd.read_csv(DATASET)
    data = add_features(raw)

    print("")
    print("=" * 80)
    print("LECH DEMAND MODEL v1")
    print(f"Dataset rows:  {len(raw)}")
    print(f"Model rows:    {len(data)}")
    print(f"Seasons:       {sorted(data['season'].unique())}")
    print("=" * 80)

    (
        pred,
        overall,
        per_season,
        development,
        last2,
        top_errors,
    ) = backtest(data)

    print("")
    print("WALK-FORWARD BACKTEST")
    print("=" * 100)

    for name, values in overall.items():
        print(
            f"{name:20}"
            f" MAE={values['mae']:8.1f}"
            f" RMSE={values['rmse']:8.1f}"
            f" MAPE={values['mape_pct']:6.2f}%"
            f" BIAS={values['bias']:8.1f}"
        )

    print("=" * 100)

    print("")
    print("PER-SEASON")

    for season, values in per_season.items():
        print(
            f"{season}: "
            f"rolling5={values['rolling_5']['mae']:.1f}, "
            f"opponent={values['opponent_scaled']['mae']:.1f}, "
            f"v1={values['p50']['mae']:.1f}, "
            f"interval={values['interval_80_coverage_pct']:.1f}%"
        )

    print("")
    print("DEVELOPMENT SEASONS")
    print(
        f"{DEVELOPMENT_SEASONS}: "
        f"v1.1 P50 MAE={development['p50']['mae']:.1f}, "
        f"interval={development['interval_80_coverage_pct']:.1f}%"
    )

    print("")
    print("HOLDOUT SEASONS — DIRECT v0.3 BENCHMARK")
    print(
        f"v0.3 MAE benchmark: {V03_LAST2_MAE:.1f}"
    )
    print(
        f"v1 P50 MAE:         "
        f"{last2['p50']['mae']:.1f}"
    )
    print(
        f"v1 bias:            "
        f"{last2['p50']['bias']:.1f}"
    )
    print(
        f"80% interval cover: "
        f"{last2['interval_80_coverage_pct']:.1f}%"
    )

    promoted = (
        last2["p50"]["mae"] < V03_LAST2_MAE
        and last2["interval_80_coverage_pct"] >= 75.0
    )

    print("")
    print(
        "DECISION: "
        + (
            "PROMOTE v1"
            if promoted
            else "HOLD — v1 does not yet beat v0.3"
        )
    )

    pred.to_csv(
        ART / "backtest_predictions_v1.csv",
        index=False,
    )

    top_errors.to_csv(
        ART / "top_errors_v1.csv",
        index=False,
    )

    # Final production candidate.
    final_calibration = calibration_from_train(
        data
    )

    final_model = build_model()

    final_model.fit(
        data[FEATURES],
        data["target_residual"],
        model__sample_weight=recency_weights(data),
    )

    package = {
        "script_version": SCRIPT_VERSION,
        "features": FEATURES,
        "categorical": CATEGORICAL,
        "numeric": NUMERIC,
        "calibration": final_calibration,
        "model": final_model,
    }

    joblib.dump(
        package,
        ART / "lech_demand_model_v1.joblib",
    )

    feature_importance(
        final_model
    ).to_csv(
        ART / "feature_importance_v1.csv",
        index=False,
    )

    report = {
        "club": "Lech Poznań",
        "dataset_rows": int(len(raw)),
        "model_rows": int(len(data)),
        "walk_forward_seasons": TEST_SEASONS,
        "overall": overall,
        "per_season": per_season,
        "development_seasons": DEVELOPMENT_SEASONS,
        "development_metrics": development,
        "holdout_seasons": HOLDOUT_SEASONS,
        "last_two_seasons": last2,
        "recency_half_life_days": RECENCY_HALF_LIFE_DAYS,
        "v03_last_two_seasons_mae": (
            V03_LAST2_MAE
        ),
        "decision": (
            "PROMOTE"
            if promoted
            else "HOLD"
        ),
        "interval_method": (
            "chronological calibration residual "
            "10th/90th percentiles"
        ),
    }

    (
        ART / "model_report_v1.json"
    ).write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("")
    print("SUCCESS")


if __name__ == "__main__":
    main()
