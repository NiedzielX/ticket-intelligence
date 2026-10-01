#!/usr/bin/env python3

import csv
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


INPUT_DIR = Path(os.getenv("EVALUATION_INPUT_DIR", "forecast_evaluation_artifacts_v01"))
OUTPUT_DIR = Path(os.getenv("CALIBRATION_OUTPUT_DIR", "calibration_report_artifacts_v01"))
EVALUATION_CSV = INPUT_DIR / "forecast_horizon_evaluation_v01.csv"
EVALUATION_SUMMARY_JSON = INPUT_DIR / "forecast_evaluation_summary_v01.json"
SHADOW_FIT_MIN_EVENTS = int(os.getenv("SHADOW_FIT_MIN_EVENTS", "3"))
SHADOW_LOEO_MIN_EVENTS = int(os.getenv("SHADOW_LOEO_MIN_EVENTS", "5"))
ACTIVATION_REVIEW_MIN_EVENTS = int(os.getenv("ACTIVATION_REVIEW_MIN_EVENTS", "6"))
SHADOW_MAX_ADJUSTMENT_RATIO = float(os.getenv("SHADOW_MAX_ADJUSTMENT_RATIO", "0.20"))
LEAGUE_COMPETITION = os.getenv("CALIBRATION_COMPETITION", "Ekstraklasa")

HORIZON_LABELS = {
    336: "T-14",
    168: "T-7",
    72: "T-72h",
    48: "T-48h",
    24: "T-24h",
}

SIGNAL_FEATURES = {
    "live_available_index": {
        "label": "Available index",
        "expected_residual_direction": "negative",
        "reason": "More remaining inventory should generally correspond to weaker demand relative to baseline.",
    },
    "live_net_removed_since_first": {
        "label": "Net removed since first",
        "expected_residual_direction": "positive",
        "reason": "More inventory removed should generally correspond to stronger demand relative to baseline.",
    },
    "live_velocity_6h": {
        "label": "6h velocity",
        "expected_residual_direction": "positive",
        "reason": "Faster recent inventory removal should generally correspond to stronger demand relative to baseline.",
    },
    "live_velocity_24h": {
        "label": "24h velocity",
        "expected_residual_direction": "positive",
        "reason": "Faster daily inventory removal should generally correspond to stronger demand relative to baseline.",
    },
    "live_acceleration_6h_vs_24h": {
        "label": "6h vs 24h acceleration",
        "expected_residual_direction": "positive",
        "reason": "Acceleration in inventory removal should generally correspond to stronger demand relative to baseline.",
    },
}

INT_FIELDS = {
    "ticket_event_id",
    "target_horizon_hours",
    "observation_id",
    "source_snapshot_id",
    "actual_attendance",
    "historical_p10",
    "historical_p50",
    "historical_p90",
    "historical_error",
    "historical_abs_error",
    "candidate_training_event_count",
    "candidate_p50",
    "candidate_error",
    "candidate_abs_error",
    "live_adjustment",
    "final_p10",
    "final_p50",
    "final_p90",
    "final_error",
    "final_abs_error",
    "live_available_total",
    "live_first_available_total",
    "live_net_removed_since_first",
    "live_net_removed_since_previous",
    "live_net_removed_6h",
    "live_net_removed_24h",
    "historical_residual_target",
}

FLOAT_FIELDS = {
    "selected_hours_to_kickoff",
    "horizon_early_gap_hours",
    "live_available_index",
    "live_velocity_since_previous",
    "live_velocity_6h",
    "live_velocity_24h",
    "live_acceleration_6h_vs_24h",
    "candidate_adjustment",
    "production_blend_weight",
}


def parse_number(value, caster):
    if value is None or value == "":
        return None
    return caster(value)


def load_evaluation_rows():
    if not EVALUATION_CSV.exists() or EVALUATION_CSV.stat().st_size == 0:
        return []

    rows = []
    with EVALUATION_CSV.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            row = dict(raw)
            for field in INT_FIELDS:
                if field in row:
                    row[field] = parse_number(row[field], int)
            for field in FLOAT_FIELDS:
                if field in row:
                    row[field] = parse_number(row[field], float)
            rows.append(row)
    return rows


def load_evaluation_summary():
    if not EVALUATION_SUMMARY_JSON.exists():
        return {}
    return json.loads(EVALUATION_SUMMARY_JSON.read_text(encoding="utf-8"))


def mean(values):
    values = [float(value) for value in values if value is not None]
    return sum(values) / len(values) if values else None


def median(values):
    values = sorted(float(value) for value in values if value is not None)
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


def pearson(x_values, y_values):
    pairs = [
        (float(x), float(y))
        for x, y in zip(x_values, y_values)
        if x is not None and y is not None
    ]
    if len(pairs) < 2:
        return None
    xs = [pair[0] for pair in pairs]
    ys = [pair[1] for pair in pairs]
    x_mean = mean(xs)
    y_mean = mean(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in pairs)
    x_var = sum((x - x_mean) ** 2 for x in xs)
    y_var = sum((y - y_mean) ** 2 for y in ys)
    denominator = math.sqrt(x_var * y_var)
    if denominator == 0:
        return None
    return numerator / denominator


def round_or_none(value, digits=4):
    return None if value is None else round(float(value), digits)


def shadow_candidate_rows(rows, feature=None):
    output = []
    for row in rows:
        if row.get("competition") != LEAGUE_COMPETITION:
            continue
        if row.get("historical_p50") is None or row.get("historical_residual_target") is None:
            continue
        if feature is not None:
            if row.get(feature) is None:
                continue
            if row.get("signal_readiness") == "data_gap":
                continue
        output.append(row)
    return output


def event_balanced_weights(rows):
    counts = defaultdict(int)
    for row in rows:
        counts[int(row["ticket_event_id"])] += 1
    return [
        1.0 / counts[int(row["ticket_event_id"])]
        for row in rows
    ]


def weighted_mean(values, weights):
    total_weight = sum(weights)
    if total_weight <= 0:
        return None
    return sum(float(value) * weight for value, weight in zip(values, weights)) / total_weight


def fit_shadow_candidate(rows, candidate_name, minimum_events=SHADOW_FIT_MIN_EVENTS):
    if candidate_name == "bias_only":
        feature = None
    elif candidate_name == "available_index_linear":
        feature = "live_available_index"
    else:
        raise ValueError(f"Unknown shadow candidate: {candidate_name}")

    usable = shadow_candidate_rows(rows, feature)
    event_count = len({int(row["ticket_event_id"]) for row in usable})
    if event_count < minimum_events:
        return None

    weights = event_balanced_weights(usable)
    residuals = [float(row["historical_residual_target"]) for row in usable]
    residual_mean = weighted_mean(residuals, weights)

    if feature is None:
        return {
            "name": candidate_name,
            "feature": None,
            "intercept": residual_mean,
            "slope": None,
            "training_event_count": event_count,
            "training_row_count": len(usable),
            "domain_direction_ok": True,
        }

    feature_values = [float(row[feature]) for row in usable]
    feature_mean = weighted_mean(feature_values, weights)
    denominator = sum(
        weight * (value - feature_mean) ** 2
        for value, weight in zip(feature_values, weights)
    )
    if denominator <= 1e-12:
        return None

    numerator = sum(
        weight * (value - feature_mean) * (residual - residual_mean)
        for value, residual, weight in zip(feature_values, residuals, weights)
    )
    slope = numerator / denominator
    intercept = residual_mean - slope * feature_mean
    return {
        "name": candidate_name,
        "feature": feature,
        "intercept": intercept,
        "slope": slope,
        "training_event_count": event_count,
        "training_row_count": len(usable),
        "domain_direction_ok": slope < 0,
    }


def shadow_prediction(model, row):
    historical_p50 = float(row["historical_p50"])
    correction = float(model["intercept"])
    if model.get("feature"):
        correction += float(model["slope"]) * float(row[model["feature"]])

    cap = abs(historical_p50) * SHADOW_MAX_ADJUSTMENT_RATIO
    capped_correction = max(-cap, min(cap, correction))
    shadow_p50 = historical_p50 + capped_correction
    actual = float(row["actual_attendance"])
    historical_abs_error = abs(actual - historical_p50)
    shadow_abs_error = abs(actual - shadow_p50)
    return {
        "raw_correction": correction,
        "capped_correction": capped_correction,
        "shadow_p50": shadow_p50,
        "historical_abs_error": historical_abs_error,
        "shadow_abs_error": shadow_abs_error,
    }


def score_shadow_candidate(model, rows):
    feature = model.get("feature")
    usable = shadow_candidate_rows(rows, feature)
    by_event = defaultdict(list)
    predictions = []

    for row in usable:
        prediction = shadow_prediction(model, row)
        event_id = int(row["ticket_event_id"])
        by_event[event_id].append(prediction)
        predictions.append((row, prediction))

    event_scores = []
    for event_id, event_predictions in sorted(by_event.items()):
        historical_mae = mean(
            prediction["historical_abs_error"]
            for prediction in event_predictions
        )
        shadow_mae = mean(
            prediction["shadow_abs_error"]
            for prediction in event_predictions
        )
        event_scores.append(
            {
                "ticket_event_id": event_id,
                "historical_mae": historical_mae,
                "shadow_mae": shadow_mae,
                "mae_improvement": historical_mae - shadow_mae,
            }
        )

    historical_mae = mean(score["historical_mae"] for score in event_scores)
    shadow_mae = mean(score["shadow_mae"] for score in event_scores)
    improved = sum(score["mae_improvement"] > 0 for score in event_scores)
    worsened = sum(score["mae_improvement"] < 0 for score in event_scores)

    return {
        "event_count": len(event_scores),
        "row_count": len(predictions),
        "historical_mae": historical_mae,
        "shadow_mae": shadow_mae,
        "mae_improvement": (
            None if historical_mae is None or shadow_mae is None
            else historical_mae - shadow_mae
        ),
        "improved_event_count": improved,
        "worsened_event_count": worsened,
        "tied_event_count": len(event_scores) - improved - worsened,
        "event_scores": event_scores,
    }


def loeo_shadow_candidate(rows, candidate_name):
    feature = None if candidate_name == "bias_only" else "live_available_index"
    usable = shadow_candidate_rows(rows, feature)
    event_ids = sorted({int(row["ticket_event_id"]) for row in usable})
    if len(event_ids) < SHADOW_LOEO_MIN_EVENTS:
        return {
            "status": "not_ready",
            "event_count": len(event_ids),
            "minimum_events": SHADOW_LOEO_MIN_EVENTS,
            "predictions": [],
        }

    event_scores = []
    predictions = []
    direction_checks = []

    for held_out_event_id in event_ids:
        train_rows = [
            row for row in usable
            if int(row["ticket_event_id"]) != held_out_event_id
        ]
        test_rows = [
            row for row in usable
            if int(row["ticket_event_id"]) == held_out_event_id
        ]
        model = fit_shadow_candidate(
            train_rows,
            candidate_name,
            minimum_events=SHADOW_FIT_MIN_EVENTS,
        )
        if model is None:
            continue
        direction_checks.append(bool(model.get("domain_direction_ok", True)))

        fold_predictions = []
        for row in test_rows:
            prediction = shadow_prediction(model, row)
            fold_predictions.append(prediction)
            predictions.append(
                {
                    "candidate": candidate_name,
                    "ticket_event_id": held_out_event_id,
                    "target_horizon_hours": int(row["target_horizon_hours"]),
                    "historical_p50": int(row["historical_p50"]),
                    "actual_attendance": int(row["actual_attendance"]),
                    "raw_correction": round_or_none(prediction["raw_correction"], 2),
                    "capped_correction": round_or_none(prediction["capped_correction"], 2),
                    "shadow_p50": round_or_none(prediction["shadow_p50"], 2),
                    "historical_abs_error": round_or_none(prediction["historical_abs_error"], 2),
                    "shadow_abs_error": round_or_none(prediction["shadow_abs_error"], 2),
                    "fold_training_event_count": int(model["training_event_count"]),
                    "fold_intercept": round_or_none(model["intercept"], 4),
                    "fold_slope": round_or_none(model.get("slope"), 4),
                }
            )

        historical_mae = mean(
            prediction["historical_abs_error"]
            for prediction in fold_predictions
        )
        shadow_mae = mean(
            prediction["shadow_abs_error"]
            for prediction in fold_predictions
        )
        event_scores.append(
            {
                "ticket_event_id": held_out_event_id,
                "historical_mae": historical_mae,
                "shadow_mae": shadow_mae,
                "mae_improvement": historical_mae - shadow_mae,
            }
        )

    historical_mae = mean(score["historical_mae"] for score in event_scores)
    shadow_mae = mean(score["shadow_mae"] for score in event_scores)
    improved = sum(score["mae_improvement"] > 0 for score in event_scores)
    worsened = sum(score["mae_improvement"] < 0 for score in event_scores)
    direction_consistent = all(direction_checks) if direction_checks else False
    improvement = (
        None if historical_mae is None or shadow_mae is None
        else historical_mae - shadow_mae
    )
    passes_guardrails = bool(
        improvement is not None
        and improvement > 0
        and improved > worsened
        and (candidate_name != "available_index_linear" or direction_consistent)
    )

    return {
        "status": "ready",
        "event_count": len(event_scores),
        "minimum_events": SHADOW_LOEO_MIN_EVENTS,
        "historical_mae": historical_mae,
        "shadow_mae": shadow_mae,
        "mae_improvement": improvement,
        "improved_event_count": improved,
        "worsened_event_count": worsened,
        "tied_event_count": len(event_scores) - improved - worsened,
        "domain_direction_consistent_across_folds": direction_consistent,
        "passes_shadow_guardrails": passes_guardrails,
        "event_scores": event_scores,
        "predictions": predictions,
    }


def build_shadow_candidates(rows, eligible_event_count):
    candidate_names = ["bias_only", "available_index_linear"]
    candidates = []
    all_predictions = []

    if eligible_event_count < SHADOW_FIT_MIN_EVENTS:
        return {
            "status": "not_ready",
            "fit_minimum_events": SHADOW_FIT_MIN_EVENTS,
            "loeo_minimum_events": SHADOW_LOEO_MIN_EVENTS,
            "activation_review_minimum_events": ACTIVATION_REVIEW_MIN_EVENTS,
            "max_adjustment_ratio": SHADOW_MAX_ADJUSTMENT_RATIO,
            "production_live_correction_active": False,
            "preferred_shadow_candidate": None,
            "activation_review_ready": False,
            "candidates": [],
            "loeo_predictions": [],
        }

    for candidate_name in candidate_names:
        model = fit_shadow_candidate(rows, candidate_name)
        if model is None:
            continue
        in_sample = score_shadow_candidate(model, rows)
        loeo = loeo_shadow_candidate(rows, candidate_name)
        all_predictions.extend(loeo.get("predictions", []))
        candidates.append(
            {
                "name": candidate_name,
                "feature": model.get("feature"),
                "intercept": round_or_none(model.get("intercept"), 4),
                "slope": round_or_none(model.get("slope"), 4),
                "domain_direction_ok": model.get("domain_direction_ok"),
                "training_event_count": model.get("training_event_count"),
                "training_row_count": model.get("training_row_count"),
                "in_sample": {
                    key: round_or_none(value, 2) if isinstance(value, float) else value
                    for key, value in in_sample.items()
                    if key != "event_scores"
                },
                "loeo": {
                    key: round_or_none(value, 2) if isinstance(value, float) else value
                    for key, value in loeo.items()
                    if key not in {"event_scores", "predictions"}
                },
            }
        )

    eligible_candidates = [
        candidate
        for candidate in candidates
        if candidate.get("loeo", {}).get("passes_shadow_guardrails")
    ]
    preferred = None
    if eligible_candidates:
        preferred = min(
            eligible_candidates,
            key=lambda candidate: candidate["loeo"]["shadow_mae"],
        )["name"]

    activation_review_ready = bool(
        eligible_event_count >= ACTIVATION_REVIEW_MIN_EVENTS
        and preferred is not None
    )

    return {
        "status": (
            "loeo_ready"
            if eligible_event_count >= SHADOW_LOEO_MIN_EVENTS
            else "fit_ready"
        ),
        "fit_minimum_events": SHADOW_FIT_MIN_EVENTS,
        "loeo_minimum_events": SHADOW_LOEO_MIN_EVENTS,
        "activation_review_minimum_events": ACTIVATION_REVIEW_MIN_EVENTS,
        "max_adjustment_ratio": SHADOW_MAX_ADJUSTMENT_RATIO,
        "production_live_correction_active": False,
        "preferred_shadow_candidate": preferred,
        "activation_review_ready": activation_review_ready,
        "candidates": candidates,
        "loeo_predictions": all_predictions,
    }


def interval_coverage(row, prefix="historical"):
    low = row.get(f"{prefix}_p10")
    high = row.get(f"{prefix}_p90")
    actual = row.get("actual_attendance")
    if low is None or high is None or actual is None:
        return None
    return int(low) <= int(actual) <= int(high)


def horizon_label(hours):
    return HORIZON_LABELS.get(int(hours), f"T-{int(hours)}h")


def build_event_reports(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[int(row["ticket_event_id"])].append(row)

    reports = []
    for ticket_event_id, event_rows in grouped.items():
        event_rows.sort(key=lambda row: int(row["target_horizon_hours"]), reverse=True)
        first = event_rows[0]
        horizons = []
        for row in event_rows:
            horizons.append(
                {
                    "horizon": horizon_label(row["target_horizon_hours"]),
                    "target_horizon_hours": int(row["target_horizon_hours"]),
                    "selected_hours_to_kickoff": row.get("selected_hours_to_kickoff"),
                    "historical_model": row.get("historical_model"),
                    "historical_p10": row.get("historical_p10"),
                    "historical_p50": row.get("historical_p50"),
                    "historical_p90": row.get("historical_p90"),
                    "historical_error": row.get("historical_error"),
                    "historical_abs_error": row.get("historical_abs_error"),
                    "historical_interval_hit": interval_coverage(row, "historical"),
                    "candidate_name": row.get("candidate_name"),
                    "candidate_training_event_count": row.get("candidate_training_event_count"),
                    "candidate_p50": row.get("candidate_p50"),
                    "candidate_adjustment": row.get("candidate_adjustment"),
                    "candidate_error": row.get("candidate_error"),
                    "candidate_abs_error": row.get("candidate_abs_error"),
                    "production_blend_weight": row.get("production_blend_weight"),
                    "live_adjustment": row.get("live_adjustment"),
                    "final_p50": row.get("final_p50"),
                    "final_error": row.get("final_error"),
                    "final_abs_error": row.get("final_abs_error"),
                    "signal_readiness": row.get("signal_readiness"),
                    "live_available_total": row.get("live_available_total"),
                    "live_available_index": row.get("live_available_index"),
                    "live_net_removed_since_first": row.get("live_net_removed_since_first"),
                    "live_velocity_6h": row.get("live_velocity_6h"),
                    "live_velocity_24h": row.get("live_velocity_24h"),
                    "live_acceleration_6h_vs_24h": row.get("live_acceleration_6h_vs_24h"),
                }
            )

        reports.append(
            {
                "ticket_event_id": ticket_event_id,
                "provider_event_id": first.get("provider_event_id"),
                "competition": first.get("competition"),
                "home_team": first.get("home_team"),
                "away_team": first.get("away_team"),
                "kickoff_at": first.get("kickoff_at"),
                "actual_attendance": first.get("actual_attendance"),
                "attendance_definition": first.get("attendance_definition"),
                "outcome_source_name": first.get("outcome_source_name"),
                "horizon_count": len(horizons),
                "horizons": horizons,
            }
        )

    reports.sort(key=lambda report: report.get("kickoff_at") or "")
    return reports


def build_horizon_summary(rows):
    grouped = defaultdict(list)
    for row in rows:
        if row.get("competition") != LEAGUE_COMPETITION:
            continue
        if row.get("historical_p50") is None:
            continue
        grouped[int(row["target_horizon_hours"])].append(row)

    summaries = []
    for target_hours in sorted(grouped.keys(), reverse=True):
        horizon_rows = grouped[target_hours]
        abs_errors = [row.get("historical_abs_error") for row in horizon_rows]
        signed_errors = [row.get("historical_error") for row in horizon_rows]
        candidate_abs_errors = [
            row.get("candidate_abs_error")
            for row in horizon_rows
            if row.get("candidate_abs_error") is not None
        ]
        final_abs_errors = [
            row.get("final_abs_error")
            for row in horizon_rows
            if row.get("final_abs_error") is not None
        ]
        coverage_values = [
            interval_coverage(row, "historical")
            for row in horizon_rows
            if interval_coverage(row, "historical") is not None
        ]
        summaries.append(
            {
                "horizon": horizon_label(target_hours),
                "target_horizon_hours": target_hours,
                "event_count": len({int(row["ticket_event_id"]) for row in horizon_rows}),
                "historical_mae": round_or_none(mean(abs_errors), 1),
                "historical_bias_actual_minus_forecast": round_or_none(mean(signed_errors), 1),
                "candidate_mae": round_or_none(mean(candidate_abs_errors), 1),
                "controlled_blend_mae": round_or_none(mean(final_abs_errors), 1),
                "historical_p10_p90_coverage": (
                    round(sum(bool(value) for value in coverage_values) / len(coverage_values), 4)
                    if coverage_values
                    else None
                ),
                "median_available_index": round_or_none(
                    median(row.get("live_available_index") for row in horizon_rows), 4
                ),
                "median_velocity_6h": round_or_none(
                    median(row.get("live_velocity_6h") for row in horizon_rows), 4
                ),
                "median_velocity_24h": round_or_none(
                    median(row.get("live_velocity_24h") for row in horizon_rows), 4
                ),
            }
        )
    return summaries


def build_signal_relationships(rows, eligible_event_count):
    league_rows = [
        row
        for row in rows
        if row.get("competition") == LEAGUE_COMPETITION
        and row.get("historical_residual_target") is not None
    ]
    grouped = defaultdict(list)
    for row in league_rows:
        grouped[int(row["target_horizon_hours"])].append(row)

    ready = eligible_event_count >= SHADOW_FIT_MIN_EVENTS
    output = []
    for target_hours in sorted(grouped.keys(), reverse=True):
        horizon_rows = grouped[target_hours]
        distinct_events = len({int(row["ticket_event_id"]) for row in horizon_rows})
        for feature_name, metadata in SIGNAL_FEATURES.items():
            pairs = [
                row
                for row in horizon_rows
                if row.get(feature_name) is not None
                and row.get("historical_residual_target") is not None
            ]
            correlation = None
            direction_match = None
            status = "insufficient_sample"
            if ready and distinct_events >= SHADOW_FIT_MIN_EVENTS and len(pairs) >= SHADOW_FIT_MIN_EVENTS:
                correlation = pearson(
                    [row.get(feature_name) for row in pairs],
                    [row.get("historical_residual_target") for row in pairs],
                )
                if correlation is not None:
                    expected_positive = metadata["expected_residual_direction"] == "positive"
                    direction_match = correlation > 0 if expected_positive else correlation < 0
                    status = "diagnostic_only"

            output.append(
                {
                    "horizon": horizon_label(target_hours),
                    "target_horizon_hours": target_hours,
                    "feature": feature_name,
                    "feature_label": metadata["label"],
                    "event_count": distinct_events,
                    "pair_count": len(pairs),
                    "expected_residual_direction": metadata["expected_residual_direction"],
                    "pearson_correlation_with_historical_residual": round_or_none(correlation, 4),
                    "correlation_sign_matches_domain_expectation": direction_match,
                    "status": status,
                    "note": (
                        metadata["reason"]
                        + " Correlation is diagnostic evidence only and must not activate a correction model by itself."
                    ),
                }
            )
    return output


def flatten_event_horizons(event_reports):
    rows = []
    for event in event_reports:
        for horizon in event["horizons"]:
            rows.append(
                {
                    "ticket_event_id": event["ticket_event_id"],
                    "provider_event_id": event["provider_event_id"],
                    "competition": event["competition"],
                    "home_team": event["home_team"],
                    "away_team": event["away_team"],
                    "kickoff_at": event["kickoff_at"],
                    "actual_attendance": event["actual_attendance"],
                    **horizon,
                }
            )
    return rows


def write_csv(path, rows):
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def fmt_number(value, digits=0):
    if value is None:
        return "—"
    if digits == 0:
        return f"{int(round(float(value))):,}".replace(",", " ")
    return f"{float(value):.{digits}f}"


def fmt_bool(value):
    if value is None:
        return "—"
    return "yes" if value else "no"


def build_markdown(report):
    calibration = report["calibration"]
    shadow = report.get("shadow_live_correction", {})
    lines = [
        "# Beyond Ticketing — Calibration Report v0.1",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "## Calibration status",
        "",
        f"Completed eligible league events: **{calibration['eligible_completed_league_events']}**",
        "",
        f"Live candidate fit gate: **{calibration['eligible_completed_league_events']} / {calibration['shadow_fit_minimum_events']} — {'READY' if calibration['ready_for_candidate_fit'] else 'NOT READY'}**",
        f"LOEO gate: **{calibration['eligible_completed_league_events']} / {calibration['loeo_minimum_events']} — {'READY' if calibration['ready_for_loeo'] else 'NOT READY'}**",
        f"Activation review gate: **{calibration['eligible_completed_league_events']} / {calibration['activation_review_minimum_events']} — {'READY' if shadow.get('activation_review_ready') else 'NOT READY'}**",
        "",
        "Production mode: **CONTROLLED LIVE BLEND** when runtime guardrails pass; otherwise historical P50 is kept.",
        "",
        "The candidate may move by up to ±20% of historical P50, but production uses only a 20% blend of that candidate correction. Full candidate activation remains disabled.",
        "",
    ]

    if not report["events"]:
        lines.extend([
            "No completed outcomes with evaluable forecast horizons are available yet.",
            "",
        ])
        return "\n".join(lines)

    lines.extend(["## Completed event reports", ""])
    for event in report["events"]:
        lines.extend(
            [
                f"### {event['home_team']} vs {event['away_team']}",
                "",
                f"Actual attendance: **{fmt_number(event['actual_attendance'])}**  ",
                f"Competition: `{event['competition'] or 'unknown'}`  ",
                f"Outcome source: `{event['outcome_source_name'] or 'unknown'}`",
                "",
                "| Horizon | Historical P50 | Candidate P50 | Production P50 | Hist. error | Candidate error | Prod. error | Available index | Readiness |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for row in event["horizons"]:
            lines.append(
                "| {horizon} | {historical_p50} | {candidate_p50} | {production_p50} | {historical_error} | {candidate_error} | {production_error} | {available_index} | {readiness} |".format(
                    horizon=row["horizon"],
                    historical_p50=fmt_number(row.get("historical_p50")),
                    candidate_p50=fmt_number(row.get("candidate_p50")),
                    production_p50=fmt_number(row.get("final_p50")),
                    historical_error=fmt_number(row.get("historical_error")),
                    candidate_error=fmt_number(row.get("candidate_error")),
                    production_error=fmt_number(row.get("final_error")),
                    available_index=fmt_number(row.get("live_available_index"), 3),
                    readiness=row.get("signal_readiness") or "—",
                )
            )
        lines.extend(
            [
                "",
                "Interpretation: this row set shows what was known at each strict as-of horizon. It does **not** claim that the live signal predicted the error before calibration is supported by enough completed events.",
                "",
            ]
        )

    lines.extend(
        [
            "## Historical baseline by horizon",
            "",
            "| Horizon | Events | Historical MAE | Candidate MAE | Controlled blend MAE | Bias (actual - forecast) | P10-P90 coverage |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in report["horizon_summary"]:
        coverage = row.get("historical_p10_p90_coverage")
        coverage_text = "—" if coverage is None else f"{coverage * 100:.1f}%"
        lines.append(
            f"| {row['horizon']} | {row['event_count']} | {fmt_number(row.get('historical_mae'))} | {fmt_number(row.get('candidate_mae'))} | {fmt_number(row.get('controlled_blend_mae'))} | {fmt_number(row.get('historical_bias_actual_minus_forecast'))} | {coverage_text} |"
        )

    lines.extend(["", "## Live signal relationships", ""])
    if not calibration["ready_for_candidate_fit"]:
        lines.append(
            f"Not scored yet. Minimum sample is {calibration['minimum_required_events']} distinct completed {LEAGUE_COMPETITION} events; current sample is {calibration['eligible_completed_league_events']}."
        )
    else:
        lines.extend(
            [
                "These are diagnostics only. They show correlation with the historical residual; they are not a production correction model.",
                "",
                "| Horizon | Feature | N | Correlation with residual | Expected sign matched |",
                "|---|---|---:|---:|:---:|",
            ]
        )
        for row in report["signal_relationships"]:
            if row["status"] != "diagnostic_only":
                continue
            lines.append(
                f"| {row['horizon']} | {row['feature_label']} | {row['pair_count']} | {fmt_number(row.get('pearson_correlation_with_historical_residual'), 3)} | {fmt_bool(row.get('correlation_sign_matches_domain_expectation'))} |"
            )

    lines.extend(["", "## Shadow live-correction candidates", ""])
    if shadow.get("status") == "not_ready":
        lines.append(
            f"No candidate is fitted yet. First live candidate fit starts at {shadow.get('fit_minimum_events', SHADOW_FIT_MIN_EVENTS)} completed league events."
        )
    else:
        lines.extend(
            [
                f"Correction cap: **±{float(shadow.get('max_adjustment_ratio', SHADOW_MAX_ADJUSTMENT_RATIO)) * 100:.0f}% of historical P50**.",
                "Candidates are event-balanced. In-sample results are exploratory; LOEO is the first out-of-sample gate.",
                "",
                "| Candidate | Train events | In-sample MAE | LOEO MAE | LOEO improvement | Events improved/worsened | Guardrails |",
                "|---|---:|---:|---:|---:|---:|---|",
            ]
        )
        for candidate in shadow.get("candidates", []):
            in_sample = candidate.get("in_sample", {})
            loeo = candidate.get("loeo", {})
            loeo_ready = loeo.get("status") == "ready"
            guardrails = (
                fmt_bool(loeo.get("passes_shadow_guardrails"))
                if loeo_ready
                else "not ready"
            )
            improved_worsened = (
                f"{loeo.get('improved_event_count', 0)}/{loeo.get('worsened_event_count', 0)}"
                if loeo_ready
                else "—"
            )
            lines.append(
                "| {name} | {events} | {in_mae} | {loeo_mae} | {improvement} | {iw} | {guardrails} |".format(
                    name=candidate["name"],
                    events=candidate.get("training_event_count", "—"),
                    in_mae=fmt_number(in_sample.get("shadow_mae")),
                    loeo_mae=fmt_number(loeo.get("shadow_mae")),
                    improvement=fmt_number(loeo.get("mae_improvement")),
                    iw=improved_worsened,
                    guardrails=guardrails,
                )
            )
        lines.extend(
            [
                "",
                f"Preferred shadow candidate: **{shadow.get('preferred_shadow_candidate') or 'none yet'}**",
                "",
            ]
        )

    lines.extend(
        [
            "",
            "## Decision rule",
            "",
            "Three completed league events allow a guarded forward live candidate and controlled 20% production blend. Five allow leave-one-event-out validation with complete events held out. Six opens a broader activation review.",
            "At runtime, a candidate is blended only when event-balanced training MAE improves, more prior events improve than worsen, and the available-index direction is sensible. Full candidate activation remains disabled.",
            "",
        ]
    )
    return "\n".join(lines)


def main():
    rows = load_evaluation_rows()
    evaluation_summary = load_evaluation_summary()

    event_reports = build_event_reports(rows)
    eligible_event_ids = {
        int(row["ticket_event_id"])
        for row in rows
        if row.get("competition") == LEAGUE_COMPETITION
        and row.get("historical_p50") is not None
        and row.get("historical_residual_target") is not None
    }
    eligible_count = len(eligible_event_ids)
    calibration = {
        "eligible_completed_league_events": eligible_count,
        "minimum_required_events": SHADOW_FIT_MIN_EVENTS,
        "shadow_fit_minimum_events": SHADOW_FIT_MIN_EVENTS,
        "loeo_minimum_events": SHADOW_LOEO_MIN_EVENTS,
        "activation_review_minimum_events": ACTIVATION_REVIEW_MIN_EVENTS,
        "ready_for_candidate_fit": eligible_count >= SHADOW_FIT_MIN_EVENTS,
        "ready_for_loeo": eligible_count >= SHADOW_LOEO_MIN_EVENTS,
        "ready_for_activation_review": eligible_count >= ACTIVATION_REVIEW_MIN_EVENTS,
        "competition": LEAGUE_COMPETITION,
        "live_correction_active": False,
        "controlled_blend_runtime_eligible": eligible_count >= SHADOW_FIT_MIN_EVENTS,
        "full_live_correction_active": False,
    }
    shadow = build_shadow_candidates(rows, eligible_count)

    report = {
        "version": "calibration-report-v0.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "event_count": len(event_reports),
        "evaluation_row_count": len(rows),
        "calibration": calibration,
        "events": event_reports,
        "horizon_summary": build_horizon_summary(rows),
        "signal_relationships": build_signal_relationships(rows, eligible_count),
        "shadow_live_correction": shadow,
        "source_evaluation_summary": evaluation_summary,
        "interpretation_policy": {
            "inventory": "demand_proxy_not_confirmed_sales",
            "pre_calibration": "show_raw_trajectory_and_historical_error_only",
            "forward_live_fit": "from three completed league events, fit simple event-balanced candidates and allow only a guarded 20% production blend",
            "loeo_validation": "from five events, validate by holding out complete events rather than individual horizon rows",
            "full_production_activation": "disabled; activation review requires a higher event gate and event-level out-of-sample improvement",
        },
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_DIR / "calibration_report_v01.json"
    markdown_path = OUTPUT_DIR / "calibration_report_v01.md"
    event_csv_path = OUTPUT_DIR / "calibration_event_horizons_v01.csv"
    horizon_csv_path = OUTPUT_DIR / "calibration_horizon_summary_v01.csv"
    signal_csv_path = OUTPUT_DIR / "calibration_signal_relationships_v01.csv"
    shadow_csv_path = OUTPUT_DIR / "calibration_shadow_loeo_predictions_v01.csv"

    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(build_markdown(report), encoding="utf-8")
    write_csv(event_csv_path, flatten_event_horizons(event_reports))
    write_csv(horizon_csv_path, report["horizon_summary"])
    write_csv(signal_csv_path, report["signal_relationships"])
    write_csv(shadow_csv_path, shadow["loeo_predictions"])

    print(f"Completed event reports: {len(event_reports)}")
    print(f"Eligible league calibration events: {eligible_count}/{SHADOW_FIT_MIN_EVENTS}")
    print(f"Candidate fit ready: {calibration['ready_for_candidate_fit']}")
    print(f"Markdown: {markdown_path}")
    print(f"JSON: {json_path}")
    print("SUCCESS")


if __name__ == "__main__":
    main()
