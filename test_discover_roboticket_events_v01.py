#!/usr/bin/env python3

import os

os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_SECRET_KEY", "")

import discover_roboticket_events_v01 as discovery


def schedule_item(text, icon):
    return {
        "text": text,
        "imgs": [
            {
                "src": icon,
                "alt": "",
                "title": "",
            }
        ],
    }


def resolved_event(event_id, opponent, match_date, kickoff_at, icon):
    return {
        "id": event_id,
        "provider": "roboticket",
        "home_team": "Lech Poznań",
        "away_team": opponent,
        "url": f"https://bilety.lechpoznan.pl/Stadium/Index?eventId={event_id}",
        "match_date": match_date,
        "kickoff_at": kickoff_at,
        "kickoff_placeholder": False,
        "competition_icon": icon,
        "schedule_text": "",
    }


def test_historical_fixture_teaches_competition_for_new_event():
    old_icon = "/media/rth_0x21_ekstraklasa.png?v=1"
    new_icon = "https://www.lechpoznan.pl/media/rth_0x21_ekstraklasa.png?v=2"
    schedule_items = [
        schedule_item(
            "20 | 09 | 2026 20:15 Lech Poznań 5:1 Radomiak Radom",
            old_icon,
        ),
        schedule_item(
            "18 | 10 | 2026 17:30 Lech Poznań -:- Korona Kielce",
            new_icon,
        ),
    ]
    existing_events = [
        {
            "external_event_id": "10235",
            "away_team": "Radomiak Radom",
            "competition": "Ekstraklasa",
            "match_date": "2026-09-20",
            "kickoff_at": "2026-09-20T20:15:00+02:00",
        },
        {
            "external_event_id": "10597",
            "away_team": "Korona Kielce",
            "competition": None,
            "match_date": "2026-10-18",
            "kickoff_at": "2026-10-18T17:30:00+02:00",
        },
    ]
    resolved = [
        resolved_event(
            "10597",
            "Korona Kielce",
            "2026-10-18",
            "2026-10-18T17:30:00+02:00",
            new_icon,
        )
    ]

    matrix, icon_map = discovery.apply_competition_mapping(
        resolved,
        existing_events,
        schedule_items,
    )

    assert matrix[0]["competition"] == "Ekstraklasa"
    assert icon_map["/media/rth_0x21_ekstraklasa.png"] == "Ekstraklasa"


def test_existing_canonical_competition_has_priority():
    schedule_items = [
        schedule_item(
            "18 | 10 | 2026 17:30 Lech Poznań -:- Korona Kielce",
            "/media/rth_0x21_unknown.png",
        )
    ]
    existing_events = [
        {
            "external_event_id": "10597",
            "away_team": "Korona Kielce",
            "competition": "Ekstraklasa",
            "match_date": "2026-10-18",
            "kickoff_at": "2026-10-18T17:30:00+02:00",
        }
    ]
    resolved = [
        resolved_event(
            "10597",
            "Korona Kielce",
            "2026-10-18",
            "2026-10-18T17:30:00+02:00",
            "/media/rth_0x21_unknown.png",
        )
    ]

    matrix, _ = discovery.apply_competition_mapping(
        resolved,
        existing_events,
        schedule_items,
    )

    assert matrix[0]["competition"] == "Ekstraklasa"


def test_ambiguous_historical_icon_is_not_inferred():
    icon = "/media/rth_0x21_shared.png"
    schedule_items = [
        schedule_item(
            "20 | 09 | 2026 20:15 Lech Poznań -:- Radomiak Radom",
            icon,
        ),
        schedule_item(
            "15 | 10 | 2026 18:45 Lech Poznań -:- Bayer 04 Leverkusen",
            icon,
        ),
        schedule_item(
            "18 | 10 | 2026 17:30 Lech Poznań -:- Korona Kielce",
            icon,
        ),
    ]
    existing_events = [
        {
            "external_event_id": "10235",
            "away_team": "Radomiak Radom",
            "competition": "Ekstraklasa",
            "match_date": "2026-09-20",
            "kickoff_at": "2026-09-20T20:15:00+02:00",
        },
        {
            "external_event_id": "99999",
            "away_team": "Bayer 04 Leverkusen",
            "competition": "UEFA Europa League",
            "match_date": "2026-10-15",
            "kickoff_at": "2026-10-15T18:45:00+02:00",
        },
    ]
    resolved = [
        resolved_event(
            "10597",
            "Korona Kielce",
            "2026-10-18",
            "2026-10-18T17:30:00+02:00",
            icon,
        )
    ]

    matrix, icon_map = discovery.apply_competition_mapping(
        resolved,
        existing_events,
        schedule_items,
    )

    assert "/media/rth_0x21_shared.png" not in icon_map
    assert matrix[0]["competition"] == ""


def main():
    test_historical_fixture_teaches_competition_for_new_event()
    test_existing_canonical_competition_has_priority()
    test_ambiguous_historical_icon_is_not_inferred()
    print("SUCCESS")


if __name__ == "__main__":
    main()
