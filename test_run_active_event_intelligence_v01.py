#!/usr/bin/env python3

import json
import tempfile
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


def main():
    test_event_environment()
    test_historical_dataset_requirement()
    test_load_active_events()
    print("SUCCESS")


if __name__ == "__main__":
    main()
