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
MIN_CALIBRATION_EVENTS = int(os.getenv("MIN_CALIBRATION_EVENTS", "7"))
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

    ready = eligible_event_count >= MIN_CALIBRATION_EVENTS
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
            if ready and distinct_events >= MIN_CALIBRATION_EVENTS and len(pairs) >= MIN_CALIBRATION_EVENTS:
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
    lines = [
        "# Beyond Ticketing — Calibration Report v0.1",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "## Calibration status",
        "",
        f"Completed eligible league events: **{calibration['eligible_completed_league_events']} / {calibration['minimum_required_events']}**",
        "",
        f"Candidate live-correction fit: **{'READY' if calibration['ready_for_candidate_fit'] else 'NOT READY'}**",
        "",
        "Live inventory is still a demand proxy, not confirmed ticket sales. No live correction is activated by this report.",
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
                "| Horizon | Historical P50 | Error (actual - forecast) | P10-P90 hit | Available index | Net removed | Vel. 6h | Vel. 24h | Accel. | Readiness |",
                "|---|---:|---:|:---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for row in event["horizons"]:
            lines.append(
                "| {horizon} | {p50} | {error} | {coverage} | {available_index} | {net_removed} | {velocity_6h} | {velocity_24h} | {acceleration} | {readiness} |".format(
                    horizon=row["horizon"],
                    p50=fmt_number(row.get("historical_p50")),
                    error=fmt_number(row.get("historical_error")),
                    coverage=fmt_bool(row.get("historical_interval_hit")),
                    available_index=fmt_number(row.get("live_available_index"), 3),
                    net_removed=fmt_number(row.get("live_net_removed_since_first")),
                    velocity_6h=fmt_number(row.get("live_velocity_6h"), 1),
                    velocity_24h=fmt_number(row.get("live_velocity_24h"), 1),
                    acceleration=fmt_number(row.get("live_acceleration_6h_vs_24h"), 1),
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
            "| Horizon | Events | Historical MAE | Bias (actual - forecast) | P10-P90 coverage |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for row in report["horizon_summary"]:
        coverage = row.get("historical_p10_p90_coverage")
        coverage_text = "—" if coverage is None else f"{coverage * 100:.1f}%"
        lines.append(
            f"| {row['horizon']} | {row['event_count']} | {fmt_number(row.get('historical_mae'))} | {fmt_number(row.get('historical_bias_actual_minus_forecast'))} | {coverage_text} |"
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

    lines.extend(
        [
            "",
            "## Decision rule",
            "",
            "Do not activate Live Correction from this report alone. A candidate model is considered only after the minimum event gate is met and must beat the historical-only baseline on event-level out-of-sample validation.",
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
        "minimum_required_events": MIN_CALIBRATION_EVENTS,
        "ready_for_candidate_fit": eligible_count >= MIN_CALIBRATION_EVENTS,
        "competition": LEAGUE_COMPETITION,
        "live_correction_active": False,
    }

    report = {
        "version": "calibration-report-v0.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "event_count": len(event_reports),
        "evaluation_row_count": len(rows),
        "calibration": calibration,
        "events": event_reports,
        "horizon_summary": build_horizon_summary(rows),
        "signal_relationships": build_signal_relationships(rows, eligible_count),
        "source_evaluation_summary": evaluation_summary,
        "interpretation_policy": {
            "inventory": "demand_proxy_not_confirmed_sales",
            "pre_calibration": "show_raw_trajectory_and_historical_error_only",
            "post_minimum_gate": "show_diagnostic_correlations_only",
            "production_activation": "requires_event_level_out_of_sample_improvement_over_historical_baseline",
        },
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_DIR / "calibration_report_v01.json"
    markdown_path = OUTPUT_DIR / "calibration_report_v01.md"
    event_csv_path = OUTPUT_DIR / "calibration_event_horizons_v01.csv"
    horizon_csv_path = OUTPUT_DIR / "calibration_horizon_summary_v01.csv"
    signal_csv_path = OUTPUT_DIR / "calibration_signal_relationships_v01.csv"

    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(build_markdown(report), encoding="utf-8")
    write_csv(event_csv_path, flatten_event_horizons(event_reports))
    write_csv(horizon_csv_path, report["horizon_summary"])
    write_csv(signal_csv_path, report["signal_relationships"])

    print(f"Completed event reports: {len(event_reports)}")
    print(f"Eligible league calibration events: {eligible_count}/{MIN_CALIBRATION_EVENTS}")
    print(f"Candidate fit ready: {calibration['ready_for_candidate_fit']}")
    print(f"Markdown: {markdown_path}")
    print(f"JSON: {json_path}")
    print("SUCCESS")


if __name__ == "__main__":
    main()
