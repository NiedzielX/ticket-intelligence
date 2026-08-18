#!/usr/bin/env python3

import build_calibration_report_v01 as report


def assert_equal(actual, expected, message):
    if actual != expected:
        raise AssertionError(f"{message}: expected={expected!r} actual={actual!r}")


def assert_close(actual, expected, tolerance, message):
    if actual is None or abs(actual - expected) > tolerance:
        raise AssertionError(f"{message}: expected≈{expected!r} actual={actual!r}")


def make_row(event_id, horizon, actual, p50, available_index, velocity_6h):
    error = actual - p50
    return {
        "ticket_event_id": event_id,
        "provider_event_id": str(10000 + event_id),
        "competition": "Ekstraklasa",
        "home_team": "Lech Poznań",
        "away_team": f"Opponent {event_id}",
        "kickoff_at": f"2026-09-{event_id:02d}T18:00:00+00:00",
        "target_horizon_hours": horizon,
        "selected_hours_to_kickoff": float(horizon) + 0.5,
        "actual_attendance": actual,
        "attendance_definition": "reported_match_attendance",
        "outcome_source_name": "test",
        "historical_model": "lech_v13_early_seasonality",
        "historical_p10": p50 - 5000,
        "historical_p50": p50,
        "historical_p90": p50 + 5000,
        "historical_error": error,
        "historical_abs_error": abs(error),
        "historical_residual_target": error,
        "final_p50": p50,
        "final_error": error,
        "final_abs_error": abs(error),
        "signal_readiness": "24h_ready",
        "live_available_total": 17000 - event_id * 100,
        "live_first_available_total": 19000,
        "live_available_index": available_index,
        "live_net_removed_since_first": 2000 + event_id * 100,
        "live_net_removed_since_previous": 10,
        "live_velocity_since_previous": velocity_6h,
        "live_net_removed_6h": 100,
        "live_velocity_6h": velocity_6h,
        "live_net_removed_24h": 300,
        "live_velocity_24h": velocity_6h / 2,
        "live_acceleration_6h_vs_24h": velocity_6h / 2,
    }


def main():
    rows = [
        make_row(1, 72, 35000, 33000, 0.70, 20.0),
        make_row(1, 24, 35000, 33500, 0.60, 30.0),
        make_row(2, 72, 30000, 32000, 0.90, 5.0),
        make_row(2, 24, 30000, 31500, 0.85, 8.0),
    ]

    events = report.build_event_reports(rows)
    assert_equal(len(events), 2, "Two event reports")
    assert_equal(events[0]["horizons"][0]["horizon"], "T-72h", "Horizon label")
    assert_equal(events[0]["horizons"][0]["historical_error"], 2000, "Residual sign")
    assert_equal(events[0]["horizons"][0]["historical_interval_hit"], True, "Interval hit")

    summary = report.build_horizon_summary(rows)
    t72 = next(row for row in summary if row["target_horizon_hours"] == 72)
    assert_equal(t72["event_count"], 2, "T-72 event count")
    assert_close(t72["historical_mae"], 2000.0, 0.01, "T-72 MAE")
    assert_close(t72["historical_bias_actual_minus_forecast"], 0.0, 0.01, "T-72 bias")

    pre_gate = report.build_signal_relationships(rows, eligible_event_count=2)
    assert_equal(
        {row["status"] for row in pre_gate},
        {"insufficient_sample"},
        "No correlation scoring before sample gate",
    )

    calibration_rows = []
    for event_id in range(1, 8):
        residual = event_id * 500
        actual = 30000 + residual
        p50 = 30000
        available_index = 1.0 - event_id * 0.05
        velocity = float(event_id * 10)
        calibration_rows.append(
            make_row(event_id, 72, actual, p50, available_index, velocity)
        )

    relationships = report.build_signal_relationships(
        calibration_rows,
        eligible_event_count=7,
    )
    available = next(
        row for row in relationships if row["feature"] == "live_available_index"
    )
    velocity = next(
        row for row in relationships if row["feature"] == "live_velocity_6h"
    )
    assert_equal(available["status"], "diagnostic_only", "Available index diagnostic status")
    assert_equal(available["correlation_sign_matches_domain_expectation"], True, "Available index expected negative sign")
    assert_equal(velocity["correlation_sign_matches_domain_expectation"], True, "Velocity expected positive sign")
    assert_close(
        available["pearson_correlation_with_historical_residual"],
        -1.0,
        0.001,
        "Available index correlation",
    )
    assert_close(
        velocity["pearson_correlation_with_historical_residual"],
        1.0,
        0.001,
        "Velocity correlation",
    )

    markdown = report.build_markdown(
        {
            "generated_at": "2026-08-18T00:00:00+00:00",
            "calibration": {
                "eligible_completed_league_events": 2,
                "minimum_required_events": 7,
                "ready_for_candidate_fit": False,
            },
            "events": events,
            "horizon_summary": summary,
            "signal_relationships": pre_gate,
        }
    )
    if "NOT READY" not in markdown:
        raise AssertionError("Markdown must expose calibration readiness")
    if "does **not** claim" not in markdown:
        raise AssertionError("Markdown must preserve pre-calibration interpretation guard")

    print("SUCCESS")


if __name__ == "__main__":
    main()
