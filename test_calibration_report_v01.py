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
        "live_available_total": int(19000 * available_index),
        "live_first_available_total": 19000,
        "live_available_index": available_index,
        "live_net_removed_since_first": int(19000 * (1.0 - available_index)),
        "live_net_removed_since_previous": 10,
        "live_velocity_since_previous": velocity_6h,
        "live_net_removed_6h": 100,
        "live_velocity_6h": velocity_6h,
        "live_net_removed_24h": 300,
        "live_velocity_24h": velocity_6h / 2,
        "live_acceleration_6h_vs_24h": velocity_6h / 2,
    }


def make_linear_shadow_rows(event_count):
    rows = []
    for event_id in range(1, event_count + 1):
        for horizon, horizon_shift in ((72, 0.00), (24, -0.03)):
            available_index = 0.95 - event_id * 0.08 + horizon_shift
            residual = int(round((0.80 - available_index) * 20000))
            p50 = 30000
            actual = p50 + residual
            rows.append(
                make_row(
                    event_id,
                    horizon,
                    actual,
                    p50,
                    available_index,
                    float(event_id * 10),
                )
            )
    return rows


def test_basic_reports():
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
        "No correlation scoring before shadow-fit gate",
    )


def test_live_candidate_fit_starts_at_three_events():
    rows = make_linear_shadow_rows(3)
    shadow = report.build_shadow_candidates(rows, eligible_event_count=3)

    assert_equal(shadow["status"], "fit_ready", "Three events enable exploratory live candidate fit")
    assert_equal(shadow["production_live_correction_active"], False, "Production remains off")
    assert_equal(shadow["preferred_shadow_candidate"], None, "No preferred candidate before LOEO")

    available = next(
        candidate for candidate in shadow["candidates"]
        if candidate["name"] == "available_index_linear"
    )
    assert_equal(available["domain_direction_ok"], True, "Available-index slope must be negative")
    assert_equal(available["loeo"]["status"], "not_ready", "LOEO stays blocked at three events")
    if available["in_sample"]["shadow_mae"] >= available["in_sample"]["historical_mae"]:
        raise AssertionError("Synthetic available-index candidate should improve in-sample MAE")

    relationships = report.build_signal_relationships(rows, eligible_event_count=3)
    available_relationship = next(
        row for row in relationships
        if row["feature"] == "live_available_index" and row["target_horizon_hours"] == 72
    )
    assert_equal(
        available_relationship["status"],
        "diagnostic_only",
        "Correlations become diagnostic at three events",
    )
    assert_equal(
        available_relationship["correlation_sign_matches_domain_expectation"],
        True,
        "Available index must match expected negative residual direction",
    )


def test_loeo_starts_at_five_events_and_holds_out_whole_events():
    rows = make_linear_shadow_rows(5)
    shadow = report.build_shadow_candidates(rows, eligible_event_count=5)

    assert_equal(shadow["status"], "loeo_ready", "Five events enable LOEO")
    available = next(
        candidate for candidate in shadow["candidates"]
        if candidate["name"] == "available_index_linear"
    )
    loeo = available["loeo"]

    assert_equal(loeo["status"], "ready", "Available-index LOEO is ready")
    assert_equal(loeo["event_count"], 5, "All five events must be held out once")
    assert_equal(loeo["improved_event_count"], 5, "Synthetic candidate should improve all events")
    assert_equal(loeo["worsened_event_count"], 0, "Synthetic candidate should worsen no events")
    assert_equal(loeo["passes_shadow_guardrails"], True, "LOEO guardrails should pass")
    if loeo["shadow_mae"] >= loeo["historical_mae"]:
        raise AssertionError("LOEO shadow MAE must beat historical MAE")

    predictions = [
        row for row in shadow["loeo_predictions"]
        if row["candidate"] == "available_index_linear"
    ]
    assert_equal(len(predictions), 10, "Two horizons per held-out event")
    assert_equal(
        {row["fold_training_event_count"] for row in predictions},
        {4},
        "Each five-event LOEO fold must train on exactly four other events",
    )
    assert_equal(
        shadow["preferred_shadow_candidate"],
        "available_index_linear",
        "Best passing LOEO candidate should become preferred shadow candidate",
    )
    assert_equal(shadow["activation_review_ready"], False, "Five events do not open activation review")


def test_activation_review_never_enables_production():
    rows = make_linear_shadow_rows(6)
    shadow = report.build_shadow_candidates(rows, eligible_event_count=6)

    assert_equal(shadow["activation_review_ready"], True, "Six events may open activation review")
    assert_equal(
        shadow["production_live_correction_active"],
        False,
        "Activation review must not enable production correction",
    )


def test_shadow_adjustment_cap():
    rows = make_linear_shadow_rows(3)
    model = {
        "name": "bias_only",
        "feature": None,
        "intercept": 20000.0,
        "slope": None,
        "training_event_count": 3,
        "training_row_count": 6,
        "domain_direction_ok": True,
    }
    row = rows[0]
    prediction = report.shadow_prediction(model, row)
    expected_cap = row["historical_p50"] * report.SHADOW_MAX_ADJUSTMENT_RATIO
    assert_close(
        prediction["capped_correction"],
        expected_cap,
        0.01,
        "Shadow correction must respect configured P50 cap",
    )


def test_markdown_exposes_staged_gates():
    rows = make_linear_shadow_rows(3)
    events = report.build_event_reports(rows)
    summary = report.build_horizon_summary(rows)
    relationships = report.build_signal_relationships(rows, eligible_event_count=3)
    shadow = report.build_shadow_candidates(rows, eligible_event_count=3)

    markdown = report.build_markdown(
        {
            "generated_at": "2026-09-29T00:00:00+00:00",
            "calibration": {
                "eligible_completed_league_events": 3,
                "minimum_required_events": 3,
                "shadow_fit_minimum_events": 3,
                "loeo_minimum_events": 5,
                "activation_review_minimum_events": 6,
                "ready_for_candidate_fit": True,
                "ready_for_loeo": False,
                "ready_for_activation_review": False,
            },
            "events": events,
            "horizon_summary": summary,
            "signal_relationships": relationships,
            "shadow_live_correction": shadow,
        }
    )
    for required in (
        "Live candidate fit gate",
        "LOEO gate",
        "Activation review gate",
        "Production mode: **CONTROLLED LIVE BLEND**",
        "Shadow live-correction candidates",
    ):
        if required not in markdown:
            raise AssertionError(f"Markdown missing staged calibration text: {required}")


def main():
    test_basic_reports()
    test_live_candidate_fit_starts_at_three_events()
    test_loeo_starts_at_five_events_and_holds_out_whole_events()
    test_activation_review_never_enables_production()
    test_shadow_adjustment_cap()
    test_markdown_exposes_staged_gates()
    print("SUCCESS")


if __name__ == "__main__":
    main()
