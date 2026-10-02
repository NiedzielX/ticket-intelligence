#!/usr/bin/env python3

import json
from pathlib import Path

import apply_roboticket_event_overrides_v01 as overrides


def assert_equal(actual, expected, message):
    if actual != expected:
        raise AssertionError(f"{message}: expected={expected!r} actual={actual!r}")


def main():
    rows = json.loads(
        Path("roboticket_event_overrides_v01.json").read_text(encoding="utf-8")
    )
    for row in rows:
        overrides.validate_override(row)

    bayer = next((row for row in rows if str(row.get("id")) == "10333"), None)
    if bayer is None:
        raise AssertionError("Missing confirmed Bayer 04 Leverkusen override 10333.")

    assert_equal(bayer["home_team"], "Lech Poznań", "Bayer event home team")
    assert_equal(bayer["away_team"], "Bayer 04 Leverkusen", "Bayer event opponent")
    assert_equal(bayer["competition"], "UEFA Europa League", "Bayer competition")
    assert_equal(bayer["match_date"], "2026-10-15", "Bayer match date")
    assert_equal(
        bayer["kickoff_at"],
        "2026-10-15T18:45:00+02:00",
        "Bayer kickoff",
    )
    assert_equal(
        bayer["url"],
        "https://bilety.lechpoznan.pl/Stadium?eventId=10333",
        "Bayer Roboticket URL",
    )
    assert_equal(bayer["mapping_confidence"], "confirmed", "Bayer mapping confidence")

    print("SUCCESS")


if __name__ == "__main__":
    main()
