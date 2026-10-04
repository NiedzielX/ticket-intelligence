#!/usr/bin/env python3

import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SECRET_KEY", "test-key")
os.environ.setdefault("EVENT_ID", "999")
os.environ.setdefault("LIVE_CANDIDATE_MIN_EVENTS", "3")
os.environ.setdefault("LIVE_BLEND_WEIGHT", "0.20")
os.environ.setdefault("RISK_ON_BLEND_WEIGHT", "0.35")
os.environ.setdefault("RISK_ON_LOEO_MIN_EVENTS", "3")
os.environ.setdefault("RISK_ON_LOEO_TRAIN_MIN_EVENTS", "2")
os.environ.setdefault("LIVE_CANDIDATE_MAX_ADJUSTMENT_RATIO", "0.20")
os.environ.setdefault("LIVE_PRODUCTION_MAX_ADJUSTMENT_RATIO", "0.10")
os.environ.setdefault("SHADOW_FIT_MIN_EVENTS", "3")
os.environ.setdefault("SHADOW_MAX_ADJUSTMENT_RATIO", "0.20")

import build_beyond_forecast_v01 as forecast


def assert_equal(actual, expected, message):
    if actual != expected:
        raise AssertionError(f"{message}: expected={expected!r} actual={actual!r}")


def assert_close(actual, expected, tolerance, message):
    if actual is None or abs(float(actual) - float(expected)) > tolerance:
        raise AssertionError(f"{message}: expected≈{expected!r} actual={actual!r}")


def training_row(event_id, horizon, available_index):
    p50 = 38000
    residual = int(round((0.80 - available_index) * 20000))
    actual = p50 + residual
    return {
        "ticket_event_id": event_id,
        "competition": "Ekstraklasa",
        "target_horizon_hours": horizon,
        "historical_p50": p50,
        "historical_residual_target": residual,
        "actual_attendance": actual,
        "signal_readiness": "24h_ready",
        "live_available_index": available_index,
    }


def synthetic_training_rows():
    rows = []
    for event_id, base_index in ((1, 0.90), (2, 0.80), (3, 0.70)):
        rows.append(training_row(event_id, 72, base_index))
        rows.append(training_row(event_id, 24, base_index - 0.03))
    return rows


def test_three_events_enable_forward_available_index_blend():
    rows = synthetic_training_rows()
    original = forecast.prior_live_correction_rows
    try:
        forecast.prior_live_correction_rows = lambda event: (rows, [1, 2, 3])
        correction = forecast.build_controlled_live_correction(
            {
                "id": 4,
                "competition": "Ekstraklasa",
                "kickoff_at": "2026-10-18T15:30:00+00:00",
            },
            {
                "status": "available",
                "p10": 30000,
                "p50": 38000,
                "p90": 43000,
            },
            {
                "signal_readiness": "24h_ready",
                "available_index": 0.95,
            },
        )
    finally:
        forecast.prior_live_correction_rows = original

    assert_equal(correction["status"], "controlled_live_blend_risk_on_v01", "Risk-on blend status")
    assert_equal(correction["selected_candidate"], "available_index_linear", "Available-index candidate")
    assert_equal(correction["training_event_count"], 3, "Three prior events")
    assert_equal(correction["training_event_ids"], [1, 2, 3], "Training event ids")
    assert_equal(correction["blend_weight"], 0.35, "Risk-on production blend weight")
    assert_equal(correction["risk_on_active"], True, "Risk-on activation")
    assert_equal(correction["candidate_p50"], 35000, "Forward candidate P50")
    assert_equal(correction["live_adjustment_applied"], -1050, "Risk-on 35% candidate correction applied")
    assert_equal(correction["production_p50"], 36950, "Risk-on production P50")
    assert_equal(correction["production_p10"], 28950, "Interval shifted consistently")
    assert_equal(correction["production_p90"], 41950, "Interval shifted consistently")

    available = next(
        item for item in correction["candidates"]
        if item["name"] == "available_index_linear"
    )
    assert_equal(available["domain_direction_ok"], True, "Available-index direction")
    assert_equal(available["runtime_guardrails_pass"], True, "Runtime guardrails")
    assert_equal(available["risk_on_guardrails_pass"], True, "Three-event LOEO guardrails")
    assert_equal(available["risk_on_loeo"]["event_count"], 3, "Three LOEO folds")
    assert_equal(
        available["risk_on_loeo"]["minimum_training_events"],
        2,
        "Each risk-on fold may train on two events",
    )


def test_risk_on_requires_24h_readiness():
    rows = synthetic_training_rows()
    original = forecast.prior_live_correction_rows
    try:
        forecast.prior_live_correction_rows = lambda event: (rows, [1, 2, 3])
        correction = forecast.build_controlled_live_correction(
            {
                "id": 4,
                "competition": "Ekstraklasa",
                "kickoff_at": "2026-10-18T15:30:00+00:00",
            },
            {
                "status": "available",
                "p10": 30000,
                "p50": 38000,
                "p90": 43000,
            },
            {
                "signal_readiness": "short_history",
                "available_index": 0.95,
            },
        )
    finally:
        forecast.prior_live_correction_rows = original

    assert_equal(correction["status"], "controlled_live_blend_v01", "Short history stays base blend")
    assert_equal(correction["risk_on_active"], False, "Risk-on blocked without 24h readiness")
    assert_equal(correction["blend_weight"], 0.20, "Base blend retained")
    assert_equal(correction["live_adjustment_applied"], -600, "Base correction applied")


def test_two_events_keep_historical_forecast():
    rows = synthetic_training_rows()[:4]
    original = forecast.prior_live_correction_rows
    try:
        forecast.prior_live_correction_rows = lambda event: (rows, [1, 2])
        correction = forecast.build_controlled_live_correction(
            {
                "id": 3,
                "competition": "Ekstraklasa",
                "kickoff_at": "2026-10-01T15:30:00+00:00",
            },
            {
                "status": "available",
                "p10": 30000,
                "p50": 38000,
                "p90": 43000,
            },
            {
                "signal_readiness": "24h_ready",
                "available_index": 0.90,
            },
        )
    finally:
        forecast.prior_live_correction_rows = original

    assert_equal(correction["status"], "insufficient_completed_events", "Two-event gate")
    assert_equal(correction["candidate_p50"], None, "No candidate before gate")
    assert_equal(correction["live_adjustment_applied"], 0, "No production adjustment")
    assert_equal(correction["production_p50"], 38000, "Historical P50 retained")


def test_data_gap_blocks_available_index_projection():
    model = {
        "name": "available_index_linear",
        "feature": "live_available_index",
        "intercept": 16000.0,
        "slope": -20000.0,
        "domain_direction_ok": True,
    }
    projection = forecast.project_live_candidate(
        model,
        38000,
        {
            "signal_readiness": "data_gap",
            "available_index": 0.95,
        },
    )
    assert_equal(projection, None, "Data gap blocks live available-index projection")


def test_forecast_reuses_prebuilt_live_features():
    payload = {
        "raw_snapshot_count": 63,
        "inventory_snapshot_count": 62,
        "feature_snapshot_count": 62,
        "excluded_anomaly_count": 0,
        "latest": {
            "snapshot_id": 123,
            "captured_at": "2026-10-04T18:00:00+00:00",
            "hours_to_kickoff": 330.0,
            "days_to_match": 13.75,
            "signal_readiness": "24h_ready",
            "data_gap_detected": False,
            "history_hours": 120.0,
            "sector_count": 40,
            "available_total": 5000,
            "first_available_total": 12000,
            "available_index": 0.416667,
            "net_removed_since_first": 7000,
            "net_removed_since_previous": 20,
            "inventory_velocity_since_previous": 40.0,
            "net_removed_6h": 300,
            "inventory_velocity_6h": 50.0,
            "net_removed_24h": 900,
            "inventory_velocity_24h": 37.5,
            "inventory_acceleration_6h_vs_24h": 12.5,
        },
    }
    original_dir = forecast.LIVE_FEATURE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        try:
            forecast.LIVE_FEATURE_DIR = Path(tmp)
            path = forecast.LIVE_FEATURE_DIR / "event_10597_latest_live_ticket_features_v01.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            signal = forecast.live_signal({"external_event_id": "10597"})
        finally:
            forecast.LIVE_FEATURE_DIR = original_dir

    assert_equal(signal["snapshot_id"], 123, "Prebuilt feature snapshot")
    assert_equal(signal["inventory_snapshot_count"], 62, "Prebuilt inventory read count")
    assert_equal(signal["feature_source"], "prebuilt_live_feature_payload", "Feature source")
    assert_equal(signal["available_index"], 0.416667, "Available index")


def test_target_and_future_events_are_excluded_from_training():
    target = {
        "id": 4,
        "competition": "Ekstraklasa",
        "kickoff_at": "2026-10-18T15:30:00+00:00",
    }
    outcomes = [
        {"ticket_event_id": event_id, "actual_attendance": 30000}
        for event_id in (1, 2, 3, 4, 5)
    ]
    events = {
        1: {"id": 1, "competition": "Ekstraklasa", "kickoff_at": "2026-08-20T17:00:00+00:00"},
        2: {"id": 2, "competition": "Ekstraklasa", "kickoff_at": "2026-09-03T18:30:00+00:00"},
        3: {"id": 3, "competition": "Ekstraklasa", "kickoff_at": "2026-09-20T18:15:00+00:00"},
        4: {"id": 4, "competition": "Ekstraklasa", "kickoff_at": "2026-10-18T15:30:00+00:00"},
        5: {"id": 5, "competition": "Ekstraklasa", "kickoff_at": "2026-11-01T17:00:00+00:00"},
    }
    captured = {}

    original_load_outcomes = forecast.evaluation_v01.load_outcomes
    original_load_events = forecast.evaluation_v01.load_events
    original_load_observations = forecast.evaluation_v01.load_observations
    original_build_evaluations = forecast.evaluation_v01.build_evaluations
    try:
        forecast.evaluation_v01.load_outcomes = lambda ticket_event_id=None: outcomes
        forecast.evaluation_v01.load_events = lambda ids: events

        def fake_observations(ids, include_payload=True):
            captured["observation_ids"] = list(ids)
            captured["include_payload"] = include_payload
            return []

        def fake_evaluations(prior_events, prior_outcomes, observations):
            captured["event_ids"] = sorted(prior_events)
            return (
                [
                    {
                        "ticket_event_id": event_id,
                        "competition": "Ekstraklasa",
                        "historical_p50": 38000,
                        "historical_residual_target": -1000,
                    }
                    for event_id in sorted(prior_events)
                ],
                [],
            )

        forecast.evaluation_v01.load_observations = fake_observations
        forecast.evaluation_v01.build_evaluations = fake_evaluations

        rows, ids = forecast.prior_live_correction_rows(target)
    finally:
        forecast.evaluation_v01.load_outcomes = original_load_outcomes
        forecast.evaluation_v01.load_events = original_load_events
        forecast.evaluation_v01.load_observations = original_load_observations
        forecast.evaluation_v01.build_evaluations = original_build_evaluations

    assert_equal(ids, [1, 2, 3], "Only prior events may train target correction")
    assert_equal(captured["event_ids"], [1, 2, 3], "Target/future events excluded before evaluation")
    assert_equal(captured["observation_ids"], [1, 2, 3], "Only prior observations loaded")
    assert_equal(captured["include_payload"], False, "Calibration reads exclude payload blobs")
    assert_equal(len(rows), 3, "Three leakage-safe training event rows")


def main():
    test_three_events_enable_forward_available_index_blend()
    test_risk_on_requires_24h_readiness()
    test_two_events_keep_historical_forecast()
    test_data_gap_blocks_available_index_projection()
    test_forecast_reuses_prebuilt_live_features()
    test_target_and_future_events_are_excluded_from_training()
    print("SUCCESS")


if __name__ == "__main__":
    main()
