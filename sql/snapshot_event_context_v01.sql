-- Roboticket 10234 was previously mislabelled as Lech - Jagiellonia.
-- Live Roboticket discovery and the official Lech schedule both confirm that
-- the normal first-team event is Lech Poznań - Raków Częstochowa,
-- 2026-09-06 17:30 Europe/Warsaw.
UPDATE ticket_events
SET
    home_team = 'Lech Poznań',
    away_team = 'Raków Częstochowa',
    competition = 'Ekstraklasa',
    match_date = DATE '2026-09-06',
    kickoff_at = TIMESTAMPTZ '2026-09-06 17:30:00+02',
    source_url = 'https://bilety.lechpoznan.pl/Stadium/Index?eventId=10234',
    mapping_source = 'roboticket_homepage+lech_official_schedule_live_2026-08-18',
    mapping_confidence = 'confirmed',
    updated_at = NOW()
WHERE provider = 'roboticket'
  AND external_event_id = '10234';

ALTER TABLE snapshots
    ADD COLUMN IF NOT EXISTS event_match_date_at_capture DATE,
    ADD COLUMN IF NOT EXISTS event_kickoff_at_capture TIMESTAMPTZ;

-- Freeze the current canonical schedule on all existing linked snapshots once.
-- Future snapshots write these values at collection time, so a later fixture
-- change cannot retroactively change historical hours_to_kickoff.
UPDATE snapshots s
SET
    event_match_date_at_capture = COALESCE(s.event_match_date_at_capture, e.match_date),
    event_kickoff_at_capture = COALESCE(s.event_kickoff_at_capture, e.kickoff_at)
FROM ticket_events e
WHERE s.ticket_event_id = e.id
  AND (
      s.event_match_date_at_capture IS NULL
      OR s.event_kickoff_at_capture IS NULL
  );

CREATE INDEX IF NOT EXISTS idx_snapshots_ticket_event_captured
    ON snapshots(ticket_event_id, captured_at);
