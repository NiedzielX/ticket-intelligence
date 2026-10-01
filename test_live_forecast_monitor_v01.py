#!/usr/bin/env python3

import os

os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SECRET_KEY", "test-key")
os.environ.setdefault("EVENT_ID", "10597")
os.environ.setdefault("FORECAST_MODEL_VERSION", "beyond-forecast-v0.2")

import build_live_forecast_monitor_v01 as monitor


def assert_equal(actual, expected, message):
    if actual != expected:
        raise AssertionError(f"{message}: expected={expected!r} actual={actual!r}")


def observation(
    row_id,
    generated_at,
    hours_to_kickoff,
    historical_p50,
    candidate_p50,
    production_p50,
    adjustment,
    available_index,
):
    candidate_adjustment = (
        None if candidate_p50 is None else candidate_p50 - historical_p50
    )
    selected = "available_index_linear" if candidate_p50 is not None else None
    return {
        "id": row_id,
        "ticket_event_id": 5,
        "source_snapshot_id": 1000 + row_id,
        "forecast_generated_at": generated_at,
        "source_snapshot_captured_at": generated_at,
        "hours_to_kickoff": hours_to_kickoff,
        "horizon": "continuous",
        "model_version": "beyond-forecast-v0.2",
        "historical_p50": historical_p50,
        "live_adjustment": adjustment,
        "final_p50": production_p50,
        "forecast_status": "historical_baseline_with_controlled_live_blend",
        "correction_status": "controlled_live_blend_v01",
        "signal_readiness": "24h_ready",
        "live_available_total": 12000 - row_id * 100,
        "live_available_index": available_index,
        "live_velocity_6h": 25.0 + row_id,
        "live_velocity_24h": 15.0 + row_id,
        "live_acceleration_6h_vs_24h": 10.0,
        "payload": {
            "live": {
                "data_gap_detected": False,
            },
            "correction": {
                "status": "controlled_live_blend_v01",
                "selected_candidate": selected,
                "candidate_p50": candidate_p50,
                "candidate_adjustment": candidate_adjustment,
                "blend_weight": 0.20 if candidate_p50 is not None else 0,
                "training_event_count": 3,
                "training_event_ids": [1, 2, 4],
                "candidates": (
                    [
                        {
                            "name": "available_index_linear",
                            "runtime_guardrails_pass": True,
                            "training_historical_mae": 9000.0,
                            "training_candidate_mae": 5000.0,
                            "training_mae_improvement": 4000.0,
                            "training_improved_event_count": 3,
                            "training_worsened_event_count": 0,
                        }
                    ]
                    if candidate_p50 is not None
                    else []
                ),
            },
        },
    }


def test_monitor_current_state_and_trajectory():
    event = {
        "id": 5,
        "external_event_id": "10597",
        "home_team": "Lech Poznań",
        "away_team": "Korona Kielce",
        "competition": "Ekstraklasa",
        "kickoff_at": "2026-10-18T15:30:00+00:00",
    }
    rows = [
        observation(
            1,
            "2026-10-01T15:00:00+00:00",
            407.5,
            38466,
            34000,
            37573,
            -893,
            0.82,
        ),
        observation(
            2,
            "2026-10-01T16:00:00+00:00",
            406.5,
            38466,
            33500,
            37473,
            -993,
            0.84,
        ),
    ]

    report = monitor.build_monitor(event, rows)
    latest = report["latest"]

    assert_equal(report["observation_count"], 2, "Observation count")
    assert_equal(latest["candidate_name"], "available_index_linear", "Candidate")
    assert_equal(latest["guardrails_pass"], True, "Guardrails")
    assert_equal(latest["candidate_p50"], 33500, "Candidate P50")
    assert_equal(latest["production_p50"], 37473, "Production P50")
    assert_equal(latest["production_adjustment"], -993, "Adjustment")
    assert_equal(latest["training_event_ids"], [1, 2, 4], "Training IDs")
    assert_equal(
        report["changes"]["candidate_p50_since_previous"],
        -500,
        "Candidate trajectory delta",
    )
    assert_equal(
        report["changes"]["production_p50_since_previous"],
        -100,
        "Production trajectory delta",
    )

    markdown = monitor.build_markdown(report)
    for required in (
        "Live Forecast Monitor",
        "Lech Poznań vs Korona Kielce",
        "Live Candidate P50",
        "Production P50",
        "available_index_linear",
        "PASS",
        "Recent trajectory",
    ):
        if required not in markdown:
            raise AssertionError(f"Markdown missing: {required}")


def test_monitor_handles_historical_fallback():
    event = {
        "id": 5,
        "external_event_id": "10597",
        "home_team": "Lech Poznań",
        "away_team": "Korona Kielce",
        "competition": "Ekstraklasa",
        "kickoff_at": "2026-10-18T15:30:00+00:00",
    }
    row = observation(
        1,
        "2026-10-01T15:00:00+00:00",
        407.5,
        38466,
        None,
        38466,
        0,
        0.82,
    )
    row["correction_status"] = "candidate_guardrails_not_met"
    row["forecast_status"] = "historical_baseline_with_live_observation"
    row["payload"]["correction"]["status"] = "candidate_guardrails_not_met"

    report = monitor.build_monitor(event, [row])
    latest = report["latest"]

    assert_equal(latest["candidate_name"], None, "No selected candidate")
    assert_equal(latest["candidate_p50"], None, "No candidate P50")
    assert_equal(latest["guardrails_pass"], False, "Fallback guardrail state")
    assert_equal(latest["production_p50"], 38466, "Historical P50 retained")


def test_empty_monitor_is_readable():
    event = {
        "id": 5,
        "external_event_id": "10597",
        "home_team": "Lech Poznań",
        "away_team": "Korona Kielce",
        "competition": "Ekstraklasa",
        "kickoff_at": "2026-10-18T15:30:00+00:00",
    }
    report = monitor.build_monitor(event, [])
    assert_equal(report["latest"], None, "No latest observation")
    markdown = monitor.build_markdown(report)
    if "No observations for this model version" not in markdown:
        raise AssertionError("Empty monitor should explain missing observations")


def main():
    test_monitor_current_state_and_trajectory()
    test_monitor_handles_historical_fallback()
    test_empty_monitor_is_readable()
    print("SUCCESS")


if __name__ == "__main__":
    main()
