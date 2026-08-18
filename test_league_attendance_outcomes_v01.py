#!/usr/bin/env python3

import os

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SECRET_KEY", "test")

import sync_league_attendance_outcomes_v01 as sync


def assert_equal(actual, expected, message):
    if actual != expected:
        raise AssertionError(f"{message}: expected={expected!r} actual={actual!r}")


def main():
    sample = (
        "Lech 3:0 Piast 25 329 2026/2027 • Ekstraklasa • 09.08.2026 17:30 "
        "Lech 0:0 Cracovia 31 310 2026/2027 • Ekstraklasa • 25.07.2026 20:15"
    )
    matches = list(sync.MATCH_RE.finditer(sample))
    assert_equal(len(matches), 2, "Parser must extract completed source rows")
    assert_equal(
        int(matches[0].group("attendance").replace(" ", "")),
        25329,
        "Piast attendance",
    )
    assert_equal(matches[0].group("opponent"), "Piast", "Piast opponent")
    assert_equal(matches[1].group("opponent"), "Cracovia", "Cracovia opponent")

    assert_equal(
        sync.team_key("Jagiellonia Białystok"),
        sync.team_key("Jagiellonia"),
        "Jagiellonia source alias",
    )
    assert_equal(
        sync.team_key("Raków Częstochowa"),
        sync.team_key("Raków"),
        "Rakow source alias",
    )
    assert_equal(
        sync.team_key("Wisła Płock"),
        sync.team_key("Wisła Pł."),
        "Wisla Plock source alias",
    )

    print("SUCCESS")


if __name__ == "__main__":
    main()
