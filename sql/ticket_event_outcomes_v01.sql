CREATE TABLE IF NOT EXISTS ticket_event_outcomes (
    ticket_event_id BIGINT PRIMARY KEY REFERENCES ticket_events(id),
    actual_attendance INTEGER NOT NULL CHECK (actual_attendance >= 0),
    attendance_definition TEXT NOT NULL DEFAULT 'official_reported_attendance',
    source_name TEXT NOT NULL,
    source_url TEXT,
    confirmed_at TIMESTAMPTZ,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ticket_event_outcomes_confirmed_at
    ON ticket_event_outcomes(confirmed_at);
