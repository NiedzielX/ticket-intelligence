#!/usr/bin/env python3

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import run_active_event_intelligence_v01 as intelligence


def test_event_environment():
    event = {
        "id": "10235",
        "competition": "Ekstraklasa",
        "away_team": "Radomiak Radom",
        "kickoff_at": "2026-09-20T18:15:00+00:00",
    }
    env = intelligence.event_environment(event, {"KEEP": "yes"})

    assert env["KEEP"] == "yes"
    assert env["EVENT_ID"] == "10235"
    assert env["EVENT_PROVIDER"] == "roboticket"
    assert env["EVENT_KICKOFF_AT"] == "2026-09-20T18:15:00+00:00"
    assert env["VALIDATE_BASELINE"] == "false"
    assert env["PERSIST_FORECAST"] == "true"
    assert env["SNAPSHOT_GAP_THRESHOLD_HOURS"] == "2.5"


def test_historical_dataset_requirement():
    assert intelligence.needs_historical_dataset(
        [{"competition": "Ekstraklasa"}]
    )
    assert not intelligence.needs_historical_dataset(
        [{"competition": "UEFA Conference League"}]
    )


def test_load_active_events():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "matrix.json"
        expected = [
            {
                "id": "10235",
                "competition": "Ekstraklasa",
                "away_team": "Radomiak Radom",
                "kickoff_at": "2026-09-20T18:15:00+00:00",
            }
        ]
        path.write_text(json.dumps(expected), encoding="utf-8")
        assert intelligence.load_active_events(path) == expected


def write_live_payload(path, captured_at):
    path.write_text(
        json.dumps(
            {
                "latest": {
                    "snapshot_id": 123,
                    "captured_at": captured_at.isoformat(),
                    "signal_readiness": "24h_ready",
                    "data_gap_detected": False,
                }
            }
        ),
        encoding="utf-8",
    )


def test_fresh_live_snapshot_is_accepted():
    now = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "latest.json"
        write_live_payload(path, now - timedelta(minutes=7))
        result = intelligence.assert_fresh_live_features(
            "10235",
            path=path,
            now=now,
            max_age_minutes=20,
        )
        assert result["snapshot_id"] == 123
        assert round(result["age_minutes"], 1) == 7.0
        assert result["signal_readiness"] == "24h_ready"


def test_stale_live_snapshot_is_rejected():
    now = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "latest.json"
        write_live_payload(path, now - timedelta(minutes=31))
        try:
            intelligence.assert_fresh_live_features(
                "10235",
                path=path,
                now=now,
                max_age_minutes=20,
            )
        except RuntimeError as exc:
            assert "stale" in str(exc).lower()
        else:
            raise AssertionError("Stale live snapshot should be rejected.")


def main():
    test_event_environment()
    test_historical_dataset_requirement()
    test_load_active_events()
    test_fresh_live_snapshot_is_accepted()
    test_stale_live_snapshot_is_rejected()
    print("SUCCESS")


if __name__ == "__main__":
    main()
