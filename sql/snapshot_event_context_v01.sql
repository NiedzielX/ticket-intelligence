ALTER TABLE snapshots
    ADD COLUMN IF NOT EXISTS event_match_date_at_capture DATE,
    ADD COLUMN IF NOT EXISTS event_kickoff_at_capture TIMESTAMPTZ;

-- Existing canonical snapshots pre-date this migration. Backfill them once from
-- the current canonical event metadata. New snapshots persist the schedule that
-- was known at capture time and are never recomputed from a later fixture update.
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
