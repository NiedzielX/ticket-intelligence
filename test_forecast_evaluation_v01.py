#!/usr/bin/env python3

import os

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SECRET_KEY", "test")

import evaluate_forecast_horizons_v01 as evaluator


def assert_equal(actual, expected, message):
    if actual != expected:
        raise AssertionError(f"{message}: expected={expected!r} actual={actual!r}")


def main():
    observations = [
        {"id": 1, "hours_to_kickoff": 73.1},
        {"id": 2, "hours_to_kickoff": 71.9},
        {"id": 3, "hours_to_kickoff": 75.0},
    ]
    selected = evaluator.select_asof_horizon(observations, 72, 2.5)
    assert_equal(selected["id"], 1, "T-72 must select the nearest pre-boundary observation")

    selected = evaluator.select_asof_horizon(
        [{"id": 4, "hours_to_kickoff": 71.99}],
        72,
        2.5,
    )
    assert_equal(selected, None, "Post-boundary observation must never be selected")

    selected = evaluator.select_asof_horizon(
        [{"id": 5, "hours_to_kickoff": 75.0}],
        72,
        2.5,
    )
    assert_equal(selected, None, "Observation outside early tolerance must be rejected")

    event = {
        "id": 3,
        "external_event_id": "10069",
        "competition": "Ekstraklasa",
        "home_team": "Lech Poznań",
        "away_team": "Jagiellonia Białystok",
        "kickoff_at": "2026-09-03T18:30:00+00:00",
    }
    outcome = {
        "actual_attendance": 35000,
        "attendance_definition": "official_reported_attendance",
        "source_name": "test",
    }
    observation = {
        "id": 10,
        "source_snapshot_id": 20,
        "hours_to_kickoff": 72.5,
        "historical_model": "lech_v13_early_seasonality",
        "historical_p50": 33000,
        "final_p50": 33000,
        "forecast_status": "historical_baseline_with_live_observation",
        "correction_status": "pending_empirical_calibration",
        "signal_readiness": "24h_ready",
    }
    row = evaluator.make_evaluation_row(event, outcome, observation, 72)
    assert_equal(row["historical_error"], 2000, "Residual target must be actual minus historical P50")
    assert_equal(row["historical_abs_error"], 2000, "Absolute historical error")
    assert_equal(row["horizon_early_gap_hours"], 0.5, "Horizon gap")

    staged_rows = []
    for event_id in range(1, 7):
        staged = dict(row)
        staged["ticket_event_id"] = event_id
        staged_rows.append(staged)

    status_four = evaluator.calibration_status(staged_rows[:4])
    assert_equal(status_four["ready_for_candidate_fit"], True, "Four events enable shadow fit")
    assert_equal(status_four["ready_for_loeo"], False, "Four events do not enable LOEO")
    assert_equal(status_four["live_correction_active"], False, "Production correction remains off")

    status_five = evaluator.calibration_status(staged_rows[:5])
    assert_equal(status_five["ready_for_loeo"], True, "Five events enable LOEO")
    assert_equal(status_five["ready_for_activation_review"], False, "Five events do not open activation review")

    status_six = evaluator.calibration_status(staged_rows)
    assert_equal(status_six["ready_for_activation_review"], True, "Six events open activation review")
    assert_equal(status_six["live_correction_active"], False, "Activation review does not enable production")

    print("SUCCESS")


if __name__ == "__main__":
    main()
